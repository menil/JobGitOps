{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    git
    gh
    just
  ];

  shellHook = ''
    echo "❄️ Welcome to the Project Template shell!"
  '';
}
