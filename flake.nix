{
  description = "SD-card / USB media ingest station: host daemon with a web display";

  # Pinned to nixos-24.11 for reproducibility; nothing here needs a newer
  # toolchain now that the RP2350 firmware is gone.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));
    in
    {
      apps = forAllSystems (pkgs:
        let
          # ./rclone.conf in the working dir wins (see .#store-rclone-config).
          useLocalRclone = ''
            if [ -z "''${RCLONE_CONFIG:-}" ] && [ -f "$PWD/rclone.conf" ]; then
              export RCLONE_CONFIG="$PWD/rclone.conf"
            fi
          '';

          # The ingest daemon: discover -> copy -> verify -> manifest -> confirm
          # -> wipe, serving its display as a web page. Split across
          # host/ingest*.py. `--dry-run` runs the full pipeline over fake cards,
          # no hardware -- open the web page to watch it.
          ingest = pkgs.writeShellApplication {
            name = "ingest";
            # rclone does copy+verify; util-linux gives mount/umount for the
            # headless auto-mount (writeShellApplication restricts PATH).
            runtimeInputs = [ pkgs.python3 pkgs.rclone pkgs.util-linux ];
            text = useLocalRclone + ''
              exec python3 ${./host}/ingest.py "$@"
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

          # Print the reader-slot mapping and exit (read-only; never copies).
          slots = pkgs.writeShellApplication {
            name = "slots";
            runtimeInputs = [ pkgs.python3 ];
            text = ''
              exec python3 ${./host}/slots.py "$@"
            '';
          };

          # Install the ingest + uploader systemd units, with the project dir
          # ($PWD, where you run this) baked in as WorkingDirectory so they read
          # ./ingest.toml and ./rclone.conf. Uses the system's sudo/systemctl.
          install-service = pkgs.writeShellScriptBin "install-service" ''
            set -eu
            prefix=innate-sd-ingester
            [ -f ./ingest.toml ] || echo "warning: no ./ingest.toml in $PWD" >&2

            render() {   # $1 = deploy source basename -> filled unit on stdout
              sed -e "s|@INGEST@|${ingest}/bin/ingest|" \
                  -e "s|@UPLOADER@|${uploader}/bin/uploader|" \
                  -e "s|@HTTP@|${http}/bin/http|" \
                  -e "s|@WORKDIR@|$PWD|" ${./deploy}/$1.service
            }
            state_of() {   # active | enabled | no  (for a unit, before we remove it)
              if systemctl is-active  --quiet "$1.service" 2>/dev/null; then echo active
              elif systemctl is-enabled --quiet "$1.service" 2>/dev/null; then echo enabled
              else echo no; fi
            }
            carry() {   # re-apply the captured state to the new $prefix-$1 unit
              case "$2" in
                active)  sudo systemctl enable --now "$prefix-$1.service" ;;
                enabled) sudo systemctl enable      "$prefix-$1.service" ;;
              esac
            }

            # Capture whether each unit is running BEFORE rewriting it, so a
            # reinstall never silently leaves the station stopped. Covers both
            # the current $prefix-* names and the old pre-rename ones.
            ingest_was=$(state_of $prefix-ingest);   [ "$ingest_was"   != no ] || ingest_was=$(state_of ingest)
            uploader_was=$(state_of $prefix-uploader); [ "$uploader_was" != no ] || uploader_was=$(state_of uploader)
            http_was=$(state_of $prefix-http);      [ "$http_was"     != no ] || http_was=$(state_of rclone-http)
            for old in ingest uploader rclone-http; do
              if [ -e /etc/systemd/system/$old.service ]; then
                echo "migrating: removing old $old.service"
                sudo systemctl disable --now "$old.service" 2>/dev/null || true
                sudo rm -f /etc/systemd/system/$old.service
              fi
            done

            echo "Installing $prefix-{ingest,uploader,http} units (config dir = $PWD; uses sudo)..."
            render ingest      | sudo tee /etc/systemd/system/$prefix-ingest.service   >/dev/null
            render uploader    | sudo tee /etc/systemd/system/$prefix-uploader.service >/dev/null
            render rclone-http | sudo tee /etc/systemd/system/$prefix-http.service     >/dev/null
            sudo systemctl daemon-reload

            carry ingest   "$ingest_was"
            carry uploader "$uploader_was"
            carry http     "$http_was"
            # daemon-reload alone does not restart a running unit onto its new
            # ExecStart, so a reinstall would otherwise keep executing the old
            # nix-store binary until someone noticed.
            for u in ingest uploader http; do
              if systemctl is-active --quiet "$prefix-$u.service"; then
                echo "restarting $prefix-$u onto the new build"
                sudo systemctl restart "$prefix-$u.service"
              fi
            done
            echo "Installed. If not already running: sudo systemctl enable --now $prefix-ingest $prefix-uploader $prefix-http"
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

          # `rclone serve http`: a read-only web listing of the backups, browsed
          # + downloaded over the LAN (the rclone-http service). target/addr come
          # from [http] in ./ingest.toml. Distinct from the station's own web
          # display ([web] addr), which is served by the ingest daemon itself.
          http = pkgs.writeShellApplication {
            name = "http";
            runtimeInputs = [ pkgs.rclone pkgs.gawk ];
            text = ''
              export RCLONE_CONFIG="''${RCLONE_CONFIG:-$PWD/rclone.conf}"
              # read [section] key, config.toml (local overrides) winning over ingest.toml
              get() {
                for f in config.toml ingest.toml; do
                  v=$(awk -v s="[$1]" -v k="$2" '
                    $0==s{inx=1;next} /^\[/{inx=0}
                    inx && $1==k {sub(/^[^=]*=[ \t]*/,"");gsub(/"/,"");sub(/[ \t]*#.*$/,"");sub(/[ \t]+$/,"");print;exit}
                  ' "$f" 2>/dev/null || true)
                  [ -n "$v" ] && { printf '%s' "$v"; return 0; }
                done
                return 0
              }
              addr=$(get http addr); [ -n "$addr" ] || addr=":8080"
              target=$(get http target)
              if [ -z "$target" ]; then
                # default: serve local dest AND cloud remote in one combined view
                localbase=$(get dest base)
                remotebase=$(get remote base)
                if [ -n "$localbase" ] && [ -n "$remotebase" ]; then
                  target=":combine,upstreams=\"local=$localbase cloud=$remotebase\":"
                else
                  target="$localbase"
                fi
              fi
              if [ -z "$target" ]; then
                echo "http: nothing to serve (set [dest] base, [remote] base, or [http] target)" >&2
                exit 1
              fi
              echo "rclone serve http: $target on $addr"
              exec rclone serve http "$target" --addr "$addr" "$@"
            '';
          };
        in
        {
          ingest = { type = "app"; program = "${ingest}/bin/ingest"; };
          uploader = { type = "app"; program = "${uploader}/bin/uploader"; };
          slots = { type = "app"; program = "${slots}/bin/slots"; };
          install-service = { type = "app"; program = "${install-service}/bin/install-service"; };
          rclone = { type = "app"; program = "${rclone}/bin/rclone"; };
          http = { type = "app"; program = "${http}/bin/http"; };
          default = { type = "app"; program = "${ingest}/bin/ingest"; };
        });

      # Tests. `nix flake check` runs them.
      checks = forAllSystems (pkgs:
        {
          # Unit tests: copier + uploader + view + web over fake card trees and
          # a real rclone against temp dirs.
          ingest-unit = pkgs.runCommand "test-ingest-unit"
            { nativeBuildInputs = [ pkgs.python3 pkgs.rclone ]; } ''
              mkdir host tests
              cp ${./host}/*.py host/
              cp ${./tests/test_ingest.py} tests/test_ingest.py
              python3 tests/test_ingest.py
              touch $out
            '';

          # End-to-end: the REAL daemon (dry-run discovery + real copier)
          # serving the REAL web display; assert the page renders a card bar.
          # This replaces the old LVGL frame-render checks.
          station-render = pkgs.runCommand "test-station-render"
            { nativeBuildInputs = [ pkgs.python3 pkgs.rclone pkgs.curl ]; } ''
              mkdir host && cp ${./host}/*.py host/
              python3 host/ingest.py --dry-run --interval-ms 100 \
                --web-addr 127.0.0.1:18081 --ticks 200 &
              pid=$!
              ok=
              for _ in $(seq 60); do
                sleep 0.25
                if curl -sf http://127.0.0.1:18081/ -o page.html \
                   && grep -q 'class="bar"' page.html; then ok=1; break; fi
              done
              kill $pid 2>/dev/null || true
              [ -n "$ok" ] || { echo "web display never rendered a card"; \
                                cat page.html 2>/dev/null; exit 1; }
              grep -q "ingest station" page.html
              touch $out
            '';
        });

      devShells = forAllSystems (pkgs:
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.rclone            # the copier/uploader shell out to it
              pkgs.curl
            ];
            shellHook = ''
              echo "SD-card ingest station dev shell"
              echo "  daemon:     nix run .#ingest -- --dry-run"
              echo "  display:    http://localhost:8081/   (while it runs)"
              echo "  slots:      nix run .#slots"
              echo "  tests:      python3 tests/test_ingest.py   (or: nix flake check)"
            '';
          };
        });
    };
}
