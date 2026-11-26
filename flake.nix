{
  description = "Sygnal v0.17.0 APNs and FCM pushkin add-ons for MindRoom rich notifications and incoming-call rings";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      forAllSystems =
        f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: {
        default = pkgs.runCommandLocal "mindroom-sygnal" { } ''
          mkdir -p "$out"
          cp ${./mindroom_pushkin.py} "$out/mindroom_pushkin.py"
          cp ${./mindroom_pushkin_loader.py} "$out/mindroom_pushkin_loader.py"
        '';
      });

      checks = forAllSystems (pkgs: {
        package-contents = pkgs.runCommandLocal "check-mindroom-sygnal" { } ''
          pkg=${self.packages.${pkgs.stdenv.hostPlatform.system}.default}
          test -f "$pkg/mindroom_pushkin.py"
          test -f "$pkg/mindroom_pushkin_loader.py"
          test ! -e "$pkg/test_mindroom_pushkin.py"
          touch "$out"
        '';
      });
    };
}
