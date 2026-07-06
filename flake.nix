{
  description = "multifunctionprinter2email";

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
          pycups
          imap-tools
          pyyaml
        ];
    in
    {
      packages = forAllSystems (pkgs: {
        default = pkgs.writers.writePython3Bin "multifunctionprinter2email" {
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
    };
}
