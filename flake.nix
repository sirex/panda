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
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pythonPackages.urwid
            pythonPackages.pytest
            pkgs.git
            pkgs.openssh
            pkgs.fzf
          ];
        };

        apps.default = {
          type = "app";
          program = "${python}/bin/python3";
          args = [ "${./panda.py}" ];
        };

        apps.tests = {
          type = "app";
          program = "${pkgs.writeShellScript "panda-tests" ''
            ${python}/bin/python3 -m pytest ${./test_panda.py} -q "$@"
          ''}";
        };
      });
}