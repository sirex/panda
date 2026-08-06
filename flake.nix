{
  description = "Panda — terminal quiz game for kids";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;
        pythonPackages = python.pkgs;
        # A python with the runtime + test deps baked in, so the flake apps
        # don't depend on the dev shell's PYTHONPATH being set.
        pythonEnv = python.withPackages (p: [ p.urwid p.pytest ]);
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pythonPackages.urwid
            pythonPackages.pytest
            pythonPackages.mypy
            pkgs.ruff
            pkgs.git
            pkgs.openssh
            pkgs.fzf
          ];
        };

        apps.default = {
          type = "app";
          program = "${pkgs.writeShellScript "panda-run" ''
            cd "${self.outPath}"
            ${pythonEnv}/bin/python3 ${./panda.py} tests
          ''}";
        };

        apps.tests = {
          type = "app";
          program = "${pkgs.writeShellScript "panda-tests" ''
            cd "${self.outPath}"
            ${pythonEnv}/bin/python3 -m pytest -q "$@"
          ''}";
        };

        apps.lint = {
          type = "app";
          program = "${pkgs.writeShellScript "panda-lint" ''
            ${pkgs.ruff}/bin/ruff check ${./panda.py} ${./test_panda.py}
            ${pkgs.ruff}/bin/ruff format --check ${./panda.py} ${./test_panda.py}
          ''}";
        };

        apps.typecheck = {
          type = "app";
          program = "${pkgs.writeShellScript "panda-typecheck" ''
            ${pythonPackages.mypy}/bin/mypy ${./panda.py}
          ''}";
        };
      });
}
