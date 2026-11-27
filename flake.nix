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
                      system.stateVersion = lib.trivial.release;
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
                }
              );

              python = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);
            in
            pkgs.runCommandLocal "check-nixos-module" { } ''
              test_tmp=$(mktemp -d "''${TMPDIR:-/tmp}/sygnal-check.XXXXXX")
              trap 'rm -rf "$test_tmp"' EXIT
              ${python}/bin/python - ${testData} ${pkgs.bash}/bin/bash ${pkgs.envsubst}/bin/envsubst "$test_tmp" <<'PY'
              import json
              import os
              import re
              import stat
              import subprocess
              import sys
              from pathlib import Path
              import yaml

              data = json.loads(Path(sys.argv[1]).read_text())
              bash_bin, envsubst_bin = sys.argv[2], sys.argv[3]
              test_tmp = Path(sys.argv[4])
              pkg = data["pkg"]
              default_image = data["defaultImage"]

              ci_images = set(
                  re.findall(
                      r"docker\.io/matrixdotorg/sygnal@sha256:[0-9a-f]{64}",
                      Path(data["ciWorkflow"]).read_text(),
                  )
              )
              assert ci_images == {default_image}, (ci_images, default_image)

              assert not data["disabledHasContainer"]
              assert not data["disabledHasSetup"]

              fa = data["failedAssertions"]
              assert any("at least one of apns.enable or fcm.enable" in m for m in fa["noPlatforms"]), fa["noPlatforms"]
              assert any("must be distinct" in m for m in fa["duplicateAppId"]), fa["duplicateAppId"]
              assert any("set envFile or provide literal values" in m for m in fa["apnsPlaceholdersNoEnv"]), fa["apnsPlaceholdersNoEnv"]

              def check_common(name, item, expected_port="127.0.0.1:5000:5000"):
                  assert item["failedAssertions"] == [], f"{name}: {item['failedAssertions']}"
                  assert item["podmanEnabled"] is True
                  assert item["ociBackend"] == "podman"
                  assert "d /run/mindroom-sygnal 0750 root root -" in item["tmpfilesRules"]
                  assert item["hasPodmanPkg"] is True

                  c = item["container"]
                  assert c["image"] == default_image
                  assert c["autoStart"] is True
                  assert c["environment"] == {
                      "PYTHONUNBUFFERED": "1",
                      "PYTHONDONTWRITEBYTECODE": "1",
                      "PYTHONPATH": "/sygnal-addons",
                      "SYGNAL_CONF": "/sygnal/sygnal.yaml",
                  }
                  assert c["ports"] == [expected_port]
                  assert c["volumes"] == ["/run/mindroom-sygnal:/sygnal:ro", f"{pkg}:/sygnal-addons:ro"]

                  setup = item["setup"]
                  assert "podman-sygnal.service" in setup["before"]
                  assert setup["serviceType"] == "oneshot"
                  assert setup["remainAfterExit"] is True
                  assert pkg in setup["restartTriggers"]
                  assert setup["templatePath"] in setup["restartTriggers"]
                  assert "install -d -m 0750 /run/mindroom-sygnal" in setup["script"]
                  assert "rm -f /run/mindroom-sygnal/mindroom_*.py /run/sygnal/mindroom_*.py" in setup["script"]
                  assert "mindroom_pushkin.py" not in setup["script"]
                  assert "mindroom_pushkin_loader.py" not in setup["script"]
                  assert "mindroom_fcm_pushkin.py" not in setup["script"]
                  subprocess.run([bash_bin, "-n"], input=setup["script"], text=True, check=True)

                  ps = item["podmanService"]
                  assert "network-online.target" in ps["after"]
                  assert "sygnal-setup.service" in ps["after"]
                  assert "sygnal-setup.service" in ps["requires"]
                  assert "network-online.target" in ps["wants"]
                  assert ps["restartTriggers"] == setup["restartTriggers"]
                  subprocess.run([bash_bin, "-n", ps["execCondition"]], check=True)

                  cond_text = Path(ps["execCondition"]).read_text()
                  template_text = Path(setup["templatePath"]).read_text()
                  parsed = yaml.safe_load(template_text)
                  assert parsed["http"] == {"bind_addresses": ["0.0.0.0"], "port": 5000}
                  assert parsed["metrics"] == {
                      "prometheus": {"enabled": False},
                      "opentracing": {"enabled": False},
                      "sentry": {"enabled": False},
                  }
                  return setup, cond_text, template_text, parsed

              # 1. APNs + FCM (matching hetzner-matrix)
              setup, cond, _, parsed = check_common("apnsAndFcm", data["apnsAndFcm"])
              assert data["extraTriggerPath"] in setup["restartTriggers"]
              assert "secret-trigger" in setup["restartTriggers"]
              assert "install -m 0400 /run/agenix/sygnal-apns-key /run/mindroom-sygnal/sygnal-apns-key.p8" in setup["script"]
              assert "install -m 0400 /run/agenix/sygnal-fcm-service-account /run/mindroom-sygnal/sygnal-fcm-service-account.json" in setup["script"]
              assert ". /run/agenix/sygnal-env" in setup["script"]
              assert f"-o /run/mindroom-sygnal/sygnal.yaml -i {setup['templatePath']}" in setup["script"]
              assert "envsubst" in setup["script"]
              assert "[ -s /run/agenix/sygnal-apns-key ]" in cond
              assert 'grep -q "CHANGE_ME" /run/agenix/sygnal-apns-key' in cond
              assert "[ -s /run/agenix/sygnal-env ]" in cond
              assert 'grep -q "^SYGNAL_APNS_KEY_ID=" /run/agenix/sygnal-env' in cond
              assert 'grep -q "CHANGE_ME" /run/agenix/sygnal-env' in cond
              assert "[ -s /run/agenix/sygnal-fcm-service-account ]" in cond
              assert 'grep -q "CHANGE_ME" /run/agenix/sygnal-fcm-service-account' in cond
              assert parsed["log"]["setup"]["root"]["level"] == "INFO"
              assert parsed["log"]["setup"]["loggers"]["sygnal.access"]["level"] == "INFO"
              assert parsed["log"]["access"]["x_forwarded_for"] is True
              assert set(parsed["apps"].keys()) == {"chat.mindroom.app", "com.mindroom_ai.app"}
              assert parsed["apps"]["chat.mindroom.app"] == {
                  "type": "mindroom_pushkin_loader.SafeApnsPushkin",
                  "keyfile": "/sygnal/sygnal-apns-key.p8",
                  "key_id": "$SYGNAL_APNS_KEY_ID",
                  "team_id": "$SYGNAL_APNS_TEAM_ID",
                  "topic": "$SYGNAL_APNS_TOPIC",
                  "platform": "$SYGNAL_APNS_PLATFORM",
                  "convert_device_token_to_hex": False,
              }
              assert parsed["apps"]["com.mindroom_ai.app"] == {
                  "type": "mindroom_pushkin_loader.SafeFcmPushkin",
                  "api_version": "v1",
                  "project_id": "mindroom-prod",
                  "service_account_file": "/sygnal/sygnal-fcm-service-account.json",
              }

              # Verify Go envsubst renders valid YAML and preserves all-digit IDs as strings
              sub_env = {
                  **os.environ,
                  "SYGNAL_APNS_KEY_ID": "0123456789",
                  "SYGNAL_APNS_TEAM_ID": "TEAM123456",
                  "SYGNAL_APNS_TOPIC": "chat.mindroom.app",
                  "SYGNAL_APNS_PLATFORM": "production",
              }
              rendered = subprocess.run(
                  [envsubst_bin, "-i", setup["templatePath"]],
                  env=sub_env,
                  capture_output=True,
                  text=True,
                  check=True,
              ).stdout
              rendered_yaml = yaml.safe_load(rendered)
              assert rendered_yaml["apps"]["chat.mindroom.app"]["key_id"] == "0123456789"
              assert isinstance(rendered_yaml["apps"]["chat.mindroom.app"]["key_id"], str)
              assert rendered_yaml["apps"]["chat.mindroom.app"]["platform"] == "production"

              # Verify runtime ExecCondition behavior (APNs+FCM with envFile + $SYGNAL_APNS_KEY_ID)
              secret_dir = test_tmp / "apns-fcm"
              secret_dir.mkdir()
              cond_copy = secret_dir / "cond.sh"
              cond_copy.write_text(cond.replace("/run/agenix", str(secret_dir)))
              cond_copy.chmod(stat.S_IRWXU)
              apns_key = secret_dir / "sygnal-apns-key"
              env_file = secret_dir / "sygnal-env"
              fcm_file = secret_dir / "sygnal-fcm-service-account"

              def run_cond(script):
                  return subprocess.run([str(script)]).returncode

              assert 1 <= run_cond(cond_copy) <= 254  # missing files
              apns_key.write_text("")
              env_file.write_text("SYGNAL_APNS_KEY_ID=ABC1234567\n")
              fcm_file.write_text('{"type": "service_account"}\n')
              assert 1 <= run_cond(cond_copy) <= 254  # empty APNs key
              apns_key.write_text("CHANGE_ME\n")
              assert 1 <= run_cond(cond_copy) <= 254  # placeholder APNs key
              apns_key.write_text("-----BEGIN PRIVATE KEY-----\n")
              env_file.write_text("")
              assert 1 <= run_cond(cond_copy) <= 254  # empty envFile
              env_file.write_text("OTHER=1\n")
              assert 1 <= run_cond(cond_copy) <= 254  # missing SYGNAL_APNS_KEY_ID
              env_file.write_text("SYGNAL_APNS_KEY_ID=CHANGE_ME\n")
              assert 1 <= run_cond(cond_copy) <= 254  # placeholder envFile
              env_file.write_text("SYGNAL_APNS_KEY_ID=ABC1234567\n")
              fcm_file.write_text('{"private_key": "CHANGE_ME"}\n')
              assert 1 <= run_cond(cond_copy) <= 254  # placeholder FCM file
              fcm_file.write_text('{"type": "service_account"}\n')
              assert run_cond(cond_copy) == 0  # all valid

              # 2. APNs-only (no envFile, custom port/logLevel/xForwardedFor, path with space)
              setup, cond, _, parsed = check_common("apnsOnly", data["apnsOnly"], expected_port="127.0.0.2:5500:5000")
              assert "install -m 0400 '/run/secrets/apns key.p8' /run/mindroom-sygnal/sygnal-apns-key.p8" in setup["script"]
              assert "rm -f /run/mindroom-sygnal/sygnal-fcm-service-account.json" in setup["script"]
              assert f"install -m 0400 {setup['templatePath']} /run/mindroom-sygnal/sygnal.yaml" in setup["script"]
              assert "envsubst" not in setup["script"]
              assert "[ -s '/run/secrets/apns key.p8' ]" in cond
              assert "SYGNAL_APNS_KEY_ID" not in cond
              assert "fcm" not in cond
              assert parsed["log"]["setup"]["root"]["level"] == "DEBUG"
              assert parsed["log"]["setup"]["loggers"]["sygnal.access"]["level"] == "DEBUG"
              assert parsed["log"]["access"]["x_forwarded_for"] is False
              assert set(parsed["apps"].keys()) == {"chat.mindroom.app"}
              assert parsed["apps"]["chat.mindroom.app"] == {
                  "type": "mindroom_pushkin_loader.SafeApnsPushkin",
                  "keyfile": "/sygnal/sygnal-apns-key.p8",
                  "key_id": "ABC1234567",
                  "team_id": "DEF8901234",
                  "topic": "chat.mindroom.app",
                  "platform": "production",
                  "convert_device_token_to_hex": True,
              }

              # 3. Mixed literal apns.keyId + apns.envFile (and pre-bracketed IPv6 listenAddress)
              setup, cond, _, parsed = check_common(
                  "apnsMixedLiteralKeyId",
                  data["apnsMixedLiteralKeyId"],
                  expected_port="[::1]:5000:5000",
              )
              assert "[ -s /run/secrets/sygnal-env ]" in cond
              assert 'grep -q "CHANGE_ME" /run/secrets/sygnal-env' in cond
              assert "SYGNAL_APNS_KEY_ID" not in cond
              assert parsed["apps"]["chat.mindroom.app"]["key_id"] == "LITERALKEY1"
              assert parsed["apps"]["chat.mindroom.app"]["team_id"] == "$SYGNAL_APNS_TEAM_ID"

              mixed_dir = test_tmp / "apns-mixed"
              mixed_dir.mkdir()
              mixed_cond = mixed_dir / "cond.sh"
              mixed_cond.write_text(cond.replace("/run/secrets", str(mixed_dir)))
              mixed_cond.chmod(stat.S_IRWXU)
              (mixed_dir / "apns.p8").write_text("-----BEGIN PRIVATE KEY-----\n")
              assert 1 <= run_cond(mixed_cond) <= 254  # missing envFile
              (mixed_dir / "sygnal-env").write_text("")
              assert 1 <= run_cond(mixed_cond) <= 254  # empty envFile
              (mixed_dir / "sygnal-env").write_text("SYGNAL_APNS_TEAM_ID=CHANGE_ME\n")
              assert 1 <= run_cond(mixed_cond) <= 254  # placeholder envFile
              (mixed_dir / "sygnal-env").write_text("SYGNAL_APNS_TEAM_ID=TEAM123456\n")
              assert run_cond(mixed_cond) == 0  # passes without SYGNAL_APNS_KEY_ID

              # 4. FCM-only (no apns options set, unbracketed IPv6 listenAddress ::1)
              setup, cond, _, parsed = check_common("fcmOnly", data["fcmOnly"], expected_port="[::1]:5000:5000")
              assert "rm -f /run/mindroom-sygnal/sygnal-apns-key.p8" in setup["script"]
              assert "install -m 0400 /run/secrets/fcm-sa.json /run/mindroom-sygnal/sygnal-fcm-service-account.json" in setup["script"]
              assert f"install -m 0400 {setup['templatePath']} /run/mindroom-sygnal/sygnal.yaml" in setup["script"]
              assert "envsubst" not in setup["script"]
              assert "[ -s /run/secrets/fcm-sa.json ]" in cond
              assert 'grep -q "CHANGE_ME" /run/secrets/fcm-sa.json' in cond
              assert "apns" not in cond
              assert "SYGNAL_APNS_KEY_ID" not in cond
              assert set(parsed["apps"].keys()) == {"com.mindroom_ai.app"}
              assert parsed["apps"]["com.mindroom_ai.app"] == {
                  "type": "mindroom_pushkin_loader.SafeFcmPushkin",
                  "api_version": "v1",
                  "project_id": "mindroom-android",
                  "service_account_file": "/sygnal/sygnal-fcm-service-account.json",
              }
              PY
              touch "$out"
            '';
        }
      );
    };
}
