{ pkgs, lib, config, inputs, ... }:

let
  # Fontconfig needs to explicitly reference fonts in the Nix store so they are discoverable.
  fontsConf = pkgs.makeFontsConf {
    fontDirectories = [
      pkgs.dejavu_fonts
      pkgs.liberation_ttf
    ];
  };
in
{
  # Expose fontconfig file path so local tooling can discover DejaVu/Liberation fonts.
  env.FONTCONFIG_FILE = fontsConf;

  # Project package tooling and font dependencies.
  packages = [
    pkgs.git
    pkgs.just
    pkgs.shellcheck
    inputs.pdf-vdiff.packages.${pkgs.stdenv.system}.default
    pkgs.dejavu_fonts
    pkgs.liberation_ttf
  ];

  # Enable Python 3.12 with automatic uv virtualenv synchronization.
  languages.python = {
    enable = true;
    version = "3.12";
    uv = {
      enable = true;
      sync.enable = true;
    };
    venv.enable = true;
  };

  # Setup shell diagnostics and automate local git hooks registration.
  enterShell = ''
    # Automatically configure local git hooks directory path.
    git config core.hooksPath .githooks

    echo "❄️ Welcome to the GitEmployed devenv shell!"
    echo "Python version: $(python --version)"
    echo "Just version: $(just --version)"
  '';
}
