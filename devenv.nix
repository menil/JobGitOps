{ pkgs, lib, config, inputs, ... }:

let
  # Fontconfig needs to explicitly reference fonts in the Nix store so they are discoverable during HTML-to-PDF rendering.
  fontsConf = pkgs.makeFontsConf {
    fontDirectories = [
      pkgs.dejavu_fonts
      pkgs.liberation_ttf
    ];
  };

  # Required because WeasyPrint dynamically loads Cairo/Pango at runtime; the dynamic linkers need explicit paths to find these library files inside the Nix store.
  weasyprintLibs = [
    pkgs.cairo
    pkgs.pango
    pkgs.gobject-introspection
    pkgs.libffi
    pkgs.fontconfig
    pkgs.glib
    pkgs.gdk-pixbuf
    pkgs.harfbuzz
  ];
in
{
  # Expose fontconfig file path so Pango/Cairo can discover local DejaVu/Liberation fonts.
  env.FONTCONFIG_FILE = fontsConf;
  
  # Set library paths for macOS dynamic linker fallback to resolve WeasyPrint's runtime CFFI loading.
  env.DYLD_FALLBACK_LIBRARY_PATH = lib.makeLibraryPath weasyprintLibs;
  
  # Set LD_LIBRARY_PATH for compatibility on Linux environments to resolve WeasyPrint CFFI loading.
  env.LD_LIBRARY_PATH = lib.makeLibraryPath weasyprintLibs;

  # Project package tooling and font dependencies. WeasyPrint C library dependencies are concatenated.
  packages = [
    pkgs.git
    pkgs.just
    pkgs.dejavu_fonts
    pkgs.liberation_ttf
  ] ++ weasyprintLibs;

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

    echo "❄️ Welcome to the JobGitOps devenv shell!"
    echo "Python version: $(python --version)"
    echo "Just version: $(just --version)"
  '';
}
