{
  description = "printer2telegram";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs?ref=nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Python dependencies shared by the package and the dev shell,
      # e.g. ps: [ ps.requests ]
      pythonDeps =
        ps: with ps; [
          sane
          pillow
          pycups
          requests
          pyyaml
        ];
    in
    {
      packages = forAllSystems (pkgs: {
        default = pkgs.writers.writePython3Bin "printer2telegram" {
          libraries = pythonDeps pkgs.python3Packages;
        } (builtins.readFile ./main.py);
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages pythonDeps)
          ];
        };
      });

      nixosModules.default =
        { config, lib, pkgs, ... }:
        let
          cfg = config.services.printer2telegram;
        in
        {
          options.services.printer2telegram = {
            enable = lib.mkEnableOption "printer2telegram, a scan-to-telegram / telegram-to-print bot";

            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
              defaultText = lib.literalExpression "printer2telegram.packages.\${system}.default";
              description = "The printer2telegram package to run.";
            };

            configFile = lib.mkOption {
              type = lib.types.path;
              example = "/etc/printer2telegram/config.yaml";
              description = ''
                Path to the yaml configuration (see config.example.yaml).
                It contains the bot token, so keep it out of the nix
                store. Root-only permissions are fine: systemd loads it
                as a credential, so the DynamicUser service can read it.
              '';
            };
          };

          config = lib.mkIf cfg.enable {
            systemd.services.printer2telegram = {
              description = "scan-to-telegram / telegram-to-print bot";
              wantedBy = [ "multi-user.target" ];
              wants = [ "network-online.target" ];
              after = [
                "network-online.target"
                "cups.service"
              ];
              serviceConfig = {
                ExecStart = "${cfg.package}/bin/printer2telegram %d/config.yaml";
                LoadCredential = "config.yaml:${cfg.configFile}";
                DynamicUser = true;
                # state.yaml (approved users, settings) lives here
                StateDirectory = "printer2telegram";
                WorkingDirectory = "%S/printer2telegram";
                Restart = "on-failure";
                RestartSec = 30;
              };
            };
          };
        };
    };
}
