{
  description = "Sygnal v0.17.0 APNs and FCM pushkin add-ons for MindRoom rich notifications and incoming-call rings";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];

      nixosModule = import ./nixos-module.nix { inherit self; };
    in
    {
      packages = lib.genAttrs systems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.runCommandLocal "mindroom-sygnal" { } ''
            mkdir -p "$out"
            cp ${./mindroom_pushkin.py} "$out/mindroom_pushkin.py"
            cp ${./mindroom_fcm_pushkin.py} "$out/mindroom_fcm_pushkin.py"
            cp ${./mindroom_pushkin_loader.py} "$out/mindroom_pushkin_loader.py"
          '';
        }
      );

      nixosModules = {
        default = nixosModule;
        mindroom-sygnal = nixosModule;
      };

      checks = lib.genAttrs systems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pkg = self.packages.${system}.default;
        in
        {
          package-contents = pkgs.runCommandLocal "check-mindroom-sygnal" { } ''
            pkg=${pkg}
            actual=$(cd "$pkg" && ls -A1 | LC_ALL=C sort)
            expected=$(printf '%s\n' mindroom_fcm_pushkin.py mindroom_pushkin.py mindroom_pushkin_loader.py)
            test "$actual" = "$expected"
            for f in $expected; do
              test -f "$pkg/$f"
            done
            test ! -e "$pkg/checks"
            test ! -e "$pkg/nixos-module.nix"
            test ! -e "$pkg/test_mindroom_pushkin.py"
            test ! -e "$pkg/test_mindroom_fcm_pushkin.py"
            touch "$out"
          '';

          image-digest = pkgs.runCommandLocal "check-sygnal-image-digest" { } ''
            module_images=$(grep -Eo 'docker\.io/matrixdotorg/sygnal@sha256:[0-9a-f]{64}' ${./nixos-module.nix} | sort -u)
            ci_images=$(grep -Eo 'docker\.io/matrixdotorg/sygnal@sha256:[0-9a-f]{64}' ${./.github/workflows/ci.yml} | sort -u)
            test -n "$module_images"
            test -n "$ci_images"
            test "$(printf '%s\n' "$module_images" | wc -l)" -eq 1
            test "$(printf '%s\n' "$ci_images" | wc -l)" -eq 1
            test "$module_images" = "$ci_images"
            touch "$out"
          '';
        }
        // lib.optionalAttrs (lib.hasSuffix "-linux" system) {
          nixos-module =
            let
              evalNixosModule =
                module: moduleConfig:
                lib.nixosSystem {
                  modules = [
                    {
                      nixpkgs.pkgs = pkgs;
                      system.stateVersion = lib.mkDefault lib.trivial.release;
                      fileSystems."/" = {
                        device = "/dev/null";
                        fsType = "ext4";
                      };
                      boot.loader.grub.enable = false;
                    }
                    module
                    moduleConfig
                  ];
                };

              failedMessages =
                eval: map (a: a.message) (builtins.filter (a: !a.assertion) eval.config.assertions);

              inspectEval =
                eval:
                let
                  cfg = eval.config;
                  sygnalSetup = cfg.systemd.services.sygnal-setup;
                  podmanSygnal = cfg.systemd.services.podman-sygnal;
                  container = cfg.virtualisation.oci-containers.containers.sygnal;
                in
                {
                  failedAssertions = failedMessages eval;
                  podmanEnabled = cfg.virtualisation.podman.enable;
                  ociBackend = cfg.virtualisation.oci-containers.backend;
                  tmpfilesRules = cfg.systemd.tmpfiles.rules;
                  hasPodmanPkg = builtins.any (p: (p.pname or "") == "podman") cfg.environment.systemPackages;
                  container = {
                    inherit (container)
                      image
                      autoStart
                      environment
                      ports
                      volumes
                      ;
                  };
                  setup = {
                    inherit (sygnalSetup) before script;
                    serviceType = sygnalSetup.serviceConfig.Type;
                    remainAfterExit = sygnalSetup.serviceConfig.RemainAfterExit;
                    runtimeDirectory = sygnalSetup.serviceConfig.RuntimeDirectory;
                    runtimeDirectoryMode = sygnalSetup.serviceConfig.RuntimeDirectoryMode;
                    restartTriggers = map toString sygnalSetup.restartTriggers;
                    templatePath = builtins.head sygnalSetup.restartTriggers;
                  };
                  podmanService = {
                    inherit (podmanSygnal) after requires wants;
                    restartTriggers = map toString podmanSygnal.restartTriggers;
                    execCondition = podmanSygnal.serviceConfig.ExecCondition;
                  };
                };

              apnsAndFcm = evalNixosModule self.nixosModules.default {
                services.mindroom-sygnal = {
                  enable = true;
                  extraRestartTriggers = [
                    ./flake.nix
                    "secret-trigger"
                  ];
                  apns = {
                    enable = true;
                    appId = "chat.mindroom.app";
                    keyFile = "/run/agenix/sygnal-apns-key";
                    envFile = "/run/agenix/sygnal-env";
                  };
                  fcm = {
                    enable = true;
                    appId = "com.mindroom_ai.app";
                    projectId = "mindroom-prod";
                    serviceAccountFile = "/run/agenix/sygnal-fcm-service-account";
                  };
                };
              };

              apnsOnly = evalNixosModule self.nixosModules.mindroom-sygnal {
                services.mindroom-sygnal = {
                  enable = true;
                  listenAddress = "127.0.0.2";
                  port = 5500;
                  logLevel = "DEBUG";
                  xForwardedFor = false;
                  apns = {
                    enable = true;
                    appId = "chat.mindroom.app";
                    keyFile = "/run/secrets/apns key.p8";
                    keyId = "ABC1234567";
                    teamId = "DEF8901234";
                    topic = "chat.mindroom.app";
                    platform = "production";
                    convertDeviceTokenToHex = true;
                  };
                };
              };

              apnsMixedLiteralKeyId = evalNixosModule self.nixosModules.default {
                services.mindroom-sygnal = {
                  enable = true;
                  listenAddress = "[::1]";
                  apns = {
                    enable = true;
                    appId = "chat.mindroom.app";
                    keyFile = "/run/secrets/apns.p8";
                    envFile = "/run/secrets/sygnal-env";
                    keyId = "LITERALKEY1";
                  };
                };
              };

              fcmOnly = evalNixosModule self.nixosModules.default {
                services.mindroom-sygnal = {
                  enable = true;
                  listenAddress = "::1";
                  fcm = {
                    enable = true;
                    appId = "com.mindroom_ai.app";
                    projectId = "mindroom-android";
                    serviceAccountFile = "/run/secrets/fcm-sa.json";
                  };
                };
              };

              disabled = evalNixosModule self.nixosModules.default { };

              noPlatforms = evalNixosModule self.nixosModules.default {
                services.mindroom-sygnal.enable = true;
              };

              duplicateAppId = evalNixosModule self.nixosModules.default {
                services.mindroom-sygnal = {
                  enable = true;
                  apns = {
                    enable = true;
                    appId = "same.app.id";
                    keyFile = "/run/secrets/apns.p8";
                    envFile = "/run/secrets/sygnal-env";
                  };
                  fcm = {
                    enable = true;
                    appId = "same.app.id";
                    projectId = "mindroom-prod";
                    serviceAccountFile = "/run/secrets/fcm.json";
                  };
                };
              };

              apnsPlaceholdersNoEnv = evalNixosModule self.nixosModules.default {
                services.mindroom-sygnal = {
                  enable = true;
                  apns = {
                    enable = true;
                    appId = "chat.mindroom.app";
                    keyFile = "/run/secrets/apns.p8";
                  };
                };
              };

              legacyDockerBackend = evalNixosModule self.nixosModules.default {
                system.stateVersion = "21.11";
                services.mindroom-sygnal = {
                  enable = true;
                  fcm = {
                    enable = true;
                    appId = "com.mindroom_ai.app";
                    projectId = "mindroom-android";
                    serviceAccountFile = "/run/secrets/fcm-sa.json";
                  };
                };
              };

              testData = pkgs.writeText "nixos-module-check.json" (
                builtins.toJSON {
                  pkg = toString pkg;
                  defaultImage = apnsAndFcm.options.services.mindroom-sygnal.image.default;
                  ciWorkflow = "${./.github/workflows/ci.yml}";
                  extraTriggerPath = toString ./flake.nix;
                  apnsAndFcm = inspectEval apnsAndFcm;
                  apnsOnly = inspectEval apnsOnly;
                  apnsMixedLiteralKeyId = inspectEval apnsMixedLiteralKeyId;
                  fcmOnly = inspectEval fcmOnly;
                  disabledHasContainer = disabled.config.virtualisation.oci-containers.containers ? sygnal;
                  disabledHasSetup = disabled.config.systemd.services ? sygnal-setup;
                  failedAssertions = {
                    noPlatforms = failedMessages noPlatforms;
                    duplicateAppId = failedMessages duplicateAppId;
                    apnsPlaceholdersNoEnv = failedMessages apnsPlaceholdersNoEnv;
                  };
                  legacyDockerBackend = {
                    ociBackend = legacyDockerBackend.config.virtualisation.oci-containers.backend;
                    failedAssertions = failedMessages legacyDockerBackend;
                  };
                }
              );

              python = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);
            in
            pkgs.runCommandLocal "check-nixos-module" { } ''
              test_tmp=$(mktemp -d "''${TMPDIR:-/tmp}/sygnal-check.XXXXXX")
              trap 'rm -rf "$test_tmp"' EXIT
              ${python}/bin/python ${./checks/nixos_module_check.py} ${testData} ${pkgs.bash}/bin/bash ${pkgs.envsubst}/bin/envsubst "$test_tmp"
              touch "$out"
            '';
        }
      );
    };
}
