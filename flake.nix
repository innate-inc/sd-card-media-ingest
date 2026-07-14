{
  description = "SD-card / USB media ingest station: RP2350-LCD-1.47 firmware, simulator, and host daemon";

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

          # Device firmware running the shared LVGL UI on the RP2350 panel.
          firmware-ui = pkgs.stdenv.mkDerivation {
            pname = "rp2350-lcd-ingest-ui";
            version = "0.1.0";
            src = ./.;
            nativeBuildInputs = [
              pkgs.cmake pkgs.python3 pkgs.gcc-arm-embedded pkgs.picotool
            ];
            dontUseCmakeConfigure = true;  # let the Pico SDK cross toolchain win
            buildPhase = ''
              runHook preBuild
              export PICO_SDK_PATH=${picoSdk}/lib/pico-sdk
              cmake -B build -S device \
                -DPICO_BOARD=pico2 \
                -DCMAKE_BUILD_TYPE=Release \
                -DLVGL_DIR=${lvgl} \
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
            meta.description = "LVGL ingest-display firmware for Waveshare RP2350-LCD-1.47";
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
          inherit firmware-ui sim;
          default = firmware-ui;
        });

      apps = forAllSystems (pkgs:
        let
          system = pkgs.stdenv.hostPlatform.system;
          pythonEnv = pkgs.python3.withPackages (ps: [ ps.pyserial ]);
          firmware-ui = self.packages.${system}.firmware-ui;

          # writeShellScriptBin (not writeShellApplication) so the *system* sudo
          # on the caller's PATH is used -- picotool needs root for raw USB
          # access (without it, some builds segfault instead of erroring). `-f`
          # reboots a running board into BOOTSEL via its reset interface.
          flash = pkgs.writeShellScriptBin "flash" ''
            uf2="${firmware-ui}/firmware.uf2"
            echo "Flashing $uf2 (via sudo picotool)..."
            exec sudo ${pkgs.picotool}/bin/picotool load -f -x "$uf2"
          '';

          # The real ingest daemon: discover -> copy -> verify -> manifest ->
          # confirm -> (dry-run) wipe, emitting the same protocol. Split across
          # host/ingest*.py; pyserial finds the device by USB VID/PID.
          # `--dry-run` runs the full pipeline over fake cards, no hardware.
          # ./rclone.conf in the working dir wins (see .#store-rclone-config).
          useLocalRclone = ''
            if [ -z "''${RCLONE_CONFIG:-}" ] && [ -f "$PWD/rclone.conf" ]; then
              export RCLONE_CONFIG="$PWD/rclone.conf"
            fi
          '';

          ingest = pkgs.writeShellApplication {
            name = "ingest";
            runtimeInputs = [ pythonEnv pkgs.rclone ];   # rclone does copy+verify
            text = useLocalRclone + ''
              exec python ${./host}/ingest.py "$@"
            '';
          };

          # Separate uploader: push verified ingests to a cloud remote (rclone),
          # decoupled from the ingest daemon. `--once` for a systemd timer.
          uploader = pkgs.writeShellApplication {
            name = "uploader";
            runtimeInputs = [ pkgs.python3 pkgs.rclone ];
            text = useLocalRclone + ''
              exec python3 ${./host}/uploader.py "$@"
            '';
          };

          # Install the ingest + uploader systemd units, with the project dir
          # ($PWD, where you run this) baked in as WorkingDirectory so they read
          # ./ingest.toml and ./rclone.conf. Uses the system's sudo/systemctl.
          install-service = pkgs.writeShellScriptBin "install-service" ''
            set -eu
            [ -f ./ingest.toml ] || echo "warning: no ./ingest.toml in $PWD" >&2
            echo "Installing ingest + uploader units (config dir = $PWD; uses sudo)..."
            for u in ingest uploader; do
              sed -e "s|@INGEST@|${ingest}/bin/ingest|" \
                  -e "s|@UPLOADER@|${uploader}/bin/uploader|" \
                  -e "s|@WORKDIR@|$PWD|" ${./deploy}/$u.service \
                | sudo tee /etc/systemd/system/$u.service >/dev/null
            done
            sudo systemctl daemon-reload
            echo "Installed. Start with:  sudo systemctl enable --now ingest uploader"
          '';

          # rclone against the project's ./rclone.conf (gitignored -- it holds
          # secrets). `nix run .#rclone -- config` sets up your remote right here;
          # ingest/uploader then auto-use the same file.
          rclone = pkgs.writeShellApplication {
            name = "rclone";
            runtimeInputs = [ pkgs.rclone ];
            text = ''
              export RCLONE_CONFIG="''${RCLONE_CONFIG:-$PWD/rclone.conf}"
              exec rclone "$@"
            '';
          };
        in
        {
          flash = { type = "app"; program = "${flash}/bin/flash"; };
          ingest = { type = "app"; program = "${ingest}/bin/ingest"; };
          uploader = { type = "app"; program = "${uploader}/bin/uploader"; };
          install-service = { type = "app"; program = "${install-service}/bin/install-service"; };
          rclone = { type = "app"; program = "${rclone}/bin/rclone"; };
          sim = { type = "app"; program = "${self.packages.${system}.sim}/bin/ingest-sim"; };
          default = self.apps.${system}.sim;
        });

      # Tests. `nix flake check` runs them.
      checks = forAllSystems (pkgs:
        let system = pkgs.stdenv.hostPlatform.system;
        in {
          # Unit test: feed fake serial lines to the parser, assert the model.
          proto = pkgs.runCommandCC "test-proto" { } ''
            gcc -I ${./app} ${./tests/test_proto.c} ${./app/proto.c} \
              -O1 -Wall -Wextra -o test
            ./test
            touch $out
          '';

          # Unit test: the ingest daemon's copier + emitter over a fake card
          # tree (verify-before-manifest, dry-run wipe, line grammar).
          ingest-unit = pkgs.runCommand "test-ingest-unit"
            { nativeBuildInputs = [ pkgs.python3 pkgs.rclone ]; } ''
              mkdir host tests
              cp ${./host}/*.py host/
              cp ${./tests/test_ingest.py} tests/test_ingest.py
              python3 tests/test_ingest.py
              touch $out
            '';

          # End-to-end: the REAL daemon (dry-run discovery + real copier) feeds
          # the REAL sim; assert the frame rendered (same check as sim-render).
          ingest-render = pkgs.runCommand "test-ingest-render"
            { nativeBuildInputs = [ pkgs.python3 pkgs.rclone ]; } ''
              python3 ${./host}/ingest.py --dry-run --interval-ms 100 --ticks 30 \
                | ${self.packages.${system}.sim}/bin/ingest-sim --shot 800 out.ppm
              python3 ${./tests/check_ppm.py} out.ppm
              touch $out
            '';

          # Smoke test: a fixed serial feed through the real LVGL sim, asserting
          # it rendered a non-blank frame (headless snapshot).
          sim-render = pkgs.runCommand "test-sim-render"
            { nativeBuildInputs = [ pkgs.python3 ]; } ''
              printf '%s\n' 'bg 202020' 'numbers 1' \
                'legend 22c35e uploaded' 'legend e69f00 uncopied' \
                'slot 0 238000 900 active 300 22c35e 200 0072b2 250 e69f00 0 0 SAND' \
                'slot 1 128000 60 active 500 22c35e 0 0 0 0 0 0 CARD2' \
                | ${self.packages.${system}.sim}/bin/ingest-sim --shot 400 out.ppm
              python3 ${./tests/check_ppm.py} out.ppm
              touch $out
            '';
        });

      devShells = forAllSystems (pkgs:
        let
          picoSdk = pkgs.pico-sdk.override { withSubmodules = true; };
          pythonEnv = pkgs.python3.withPackages (ps: [ ps.pyserial ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.cmake
              pkgs.gcc-arm-embedded
              pkgs.picotool
              pkgs.python3
              pythonEnv
              pkgs.rclone            # the copier/uploader shell out to it
              picoSdk
            ];
            PICO_SDK_PATH = "${picoSdk}/lib/pico-sdk";
            shellHook = ''
              echo "SD-card ingest station dev shell"
              echo "  device fw:  nix build .#firmware-ui   (-> ./result/firmware.uf2)"
              echo "  flash:      nix run .#flash"
              echo "  simulator:  nix run .#sim"
              echo "  daemon:     nix run .#ingest -- --dry-run | nix run .#sim"
              echo "  tests:      python3 tests/test_ingest.py   (or: nix flake check)"
            '';
          };
        });
    };
}
