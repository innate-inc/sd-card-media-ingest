{
  description = "Reproducible firmware + tooling to display USB-serial images on the Waveshare RP2350-LCD-1.47";

  # Pinned to nixos-24.11: it ships pico-sdk 2.x / picotool 2.x (the first
  # releases with RP2350 support) and still evaluates on older Nix versions.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";

  # LVGL: the on-device UI framework (also builds the desktop simulator).
  inputs.lvgl = {
    url = "github:lvgl/lvgl/v9.2.2";
    flake = false;
  };

  outputs = { self, nixpkgs, lvgl }:
    let
      # The board only builds on Linux hosts that Nix can cross-compile from.
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));
    in
    {
      packages = forAllSystems (pkgs:
        let
          # tinyusb (and friends) live in SDK submodules; stdio-over-USB needs it.
          picoSdk = pkgs.pico-sdk.override { withSubmodules = true; };

          firmware = pkgs.stdenv.mkDerivation {
            pname = "rp2350-lcd-usb-image";
            version = "1.0.0";
            src = ./firmware;

            nativeBuildInputs = [
              pkgs.cmake
              pkgs.python3
              pkgs.gcc-arm-embedded # arm-none-eabi toolchain for RP2350 (Cortex-M33)
              pkgs.picotool         # SDK 2.x uses picotool to emit the .uf2
            ];

            # Drive cmake by hand: the generic nixpkgs cmake configure hook
            # forces -DCMAKE_C_COMPILER=gcc (host gcc), which clobbers the Pico
            # SDK's arm-none-eabi cross toolchain. Bypassing it lets the SDK's
            # toolchain file win.
            dontUseCmakeConfigure = true;

            buildPhase = ''
              runHook preBuild
              export PICO_SDK_PATH=${picoSdk}/lib/pico-sdk
              cmake -B build -S . \
                -DPICO_BOARD=pico2 \
                -DCMAKE_BUILD_TYPE=Release \
                -Dpicotool_DIR=${pkgs.picotool}/lib/cmake/picotool
              cmake --build build -j"$NIX_BUILD_CORES"
              runHook postBuild
            '';

            installPhase = ''
              runHook preInstall
              mkdir -p $out
              cp build/firmware.uf2 build/firmware.elf $out/
              runHook postInstall
            '';

            meta = {
              description = "USB-serial image display firmware for Waveshare RP2350-LCD-1.47";
              platforms = systems;
            };
          };

          # Desktop simulator: the exact LVGL UI in an SDL window.
          sim = pkgs.stdenv.mkDerivation {
            pname = "ingest-sim";
            version = "0.1.0";
            src = ./.;
            nativeBuildInputs = [ pkgs.cmake pkgs.pkg-config ];
            buildInputs = [ pkgs.SDL2 ];
            cmakeDir = "../sim";
            cmakeFlags = [ "-DLVGL_DIR=${lvgl}" ];
            meta.description = "SDL desktop simulator for the ingest display UI";
          };
        in
        {
          inherit firmware sim;
          default = firmware;
        });

      apps = forAllSystems (pkgs:
        let
          system = pkgs.stdenv.hostPlatform.system;
          firmware = self.packages.${system}.firmware;

          # Host-side sender: any image -> letterboxed 172x320 RGB565 -> serial.
          pythonEnv = pkgs.python3.withPackages (ps: [ ps.pillow ps.pyserial ]);

          flash = pkgs.writeShellApplication {
            name = "flash";
            runtimeInputs = [ pkgs.picotool ];
            text = ''
              # Put the board in BOOTSEL (hold BOOT while plugging in) OR rely on
              # picotool -f to reboot a running board that exposes the reset iface.
              echo "Flashing ${firmware}/firmware.uf2 ..."
              picotool load -f -x "${firmware}/firmware.uf2"
            '';
          };

          send = pkgs.writeShellApplication {
            name = "send";
            runtimeInputs = [ pythonEnv ];
            text = ''
              exec python ${./host/send_image.py} "$@"
            '';
          };
        in
        {
          flash = { type = "app"; program = "${flash}/bin/flash"; };
          send = { type = "app"; program = "${send}/bin/send"; };
          sim = { type = "app"; program = "${self.packages.${system}.sim}/bin/ingest-sim"; };
          default = self.apps.${system}.sim;
        });

      devShells = forAllSystems (pkgs:
        let
          picoSdk = pkgs.pico-sdk.override { withSubmodules = true; };
          pythonEnv = pkgs.python3.withPackages (ps: [ ps.pillow ps.pyserial ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.cmake
              pkgs.gcc-arm-embedded
              pkgs.picotool
              pkgs.python3
              pythonEnv
              picoSdk
            ];
            PICO_SDK_PATH = "${picoSdk}/lib/pico-sdk";
            shellHook = ''
              echo "RP2350-LCD-1.47 dev shell"
              echo "  build:  cmake -S firmware -B build && cmake --build build"
              echo "  or:     nix build .#firmware   (-> ./result/firmware.uf2)"
              echo "  flash:  nix run .#flash"
              echo "  send:   nix run .#send -- IMAGE [--port /dev/ttyACM0]"
            '';
          };
        });
    };
}
