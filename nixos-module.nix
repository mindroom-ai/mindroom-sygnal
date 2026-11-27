{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  inherit (lib) mkEnableOption mkOption types;
  mkOpt =
    type: default: description:
    mkOption { inherit type default description; };
  mkReqOpt = type: description: mkOption { inherit type description; };
  pathOrStr = types.either types.path types.str;

  cfg = config.services.mindroom-sygnal;
  useApnsEnvFile = cfg.apns.enable && cfg.apns.envFile != null;
  apnsKeyFile = lib.escapeShellArg (toString cfg.apns.keyFile);
  apnsEnvFile = lib.escapeShellArg (toString cfg.apns.envFile);
  fcmServiceAccountFile = lib.escapeShellArg (toString cfg.fcm.serviceAccountFile);

  formattedListenAddress =
    if lib.hasInfix ":" cfg.listenAddress && !(lib.hasPrefix "[" cfg.listenAddress) then
      "[${cfg.listenAddress}]"
    else
      cfg.listenAddress;

  appLines =
    lib.optionals cfg.apns.enable [
      "  ${builtins.toJSON cfg.apns.appId}:"
      "    type: \"mindroom_pushkin_loader.SafeApnsPushkin\""
      "    keyfile: \"/sygnal/sygnal-apns-key.p8\""
      "    key_id: ${builtins.toJSON cfg.apns.keyId}"
      "    team_id: ${builtins.toJSON cfg.apns.teamId}"
      "    topic: ${builtins.toJSON cfg.apns.topic}"
      "    platform: ${builtins.toJSON cfg.apns.platform}"
      "    convert_device_token_to_hex: ${lib.boolToString cfg.apns.convertDeviceTokenToHex}"
    ]
    ++ lib.optionals cfg.fcm.enable [
      "  ${builtins.toJSON cfg.fcm.appId}:"
      "    type: \"mindroom_pushkin_loader.SafeFcmPushkin\""
      "    api_version: \"v1\""
      "    project_id: ${builtins.toJSON cfg.fcm.projectId}"
      "    service_account_file: \"/sygnal/sygnal-fcm-service-account.json\""
    ];

  sygnalConfigTemplate = pkgs.writeText "sygnal.yaml" ''
    log:
      setup:
        version: 1
        formatters:
          normal:
            format: "%(asctime)s [%(process)d] %(levelname)-5s %(name)s %(message)s"
        handlers:
          stderr:
            class: "logging.StreamHandler"
            formatter: "normal"
            stream: "ext://sys.stderr"
          stdout:
            class: "logging.StreamHandler"
            formatter: "normal"
            stream: "ext://sys.stdout"
        loggers:
          sygnal.access:
            propagate: false
            handlers: ["stdout"]
            level: ${builtins.toJSON cfg.logLevel}
          sygnal:
            propagate: false
            handlers: ["stderr"]
        root:
          handlers: ["stderr"]
          level: ${builtins.toJSON cfg.logLevel}
        disable_existing_loggers: false
      access:
        x_forwarded_for: ${lib.boolToString cfg.xForwardedFor}

    http:
      bind_addresses: ["0.0.0.0"]
      port: 5000

    metrics:
      prometheus:
        enabled: false
      opentracing:
        enabled: false
      sentry:
        enabled: false

    apps:
    ${lib.concatStringsSep "\n" appLines}
  '';

  sygnalRestartTriggers = [
    sygnalConfigTemplate
    cfg.package
  ]
  ++ cfg.extraRestartTriggers;

  execConditionChecks =
    lib.optionals cfg.apns.enable [
      "[ -s ${apnsKeyFile} ]"
      ''! ${pkgs.gnugrep}/bin/grep -q "CHANGE_ME" ${apnsKeyFile}''
    ]
    ++ lib.optionals useApnsEnvFile (
      [ "[ -s ${apnsEnvFile} ]" ]
      ++ lib.optional (
        cfg.apns.keyId == "$SYGNAL_APNS_KEY_ID"
      ) ''${pkgs.gnugrep}/bin/grep -q "^SYGNAL_APNS_KEY_ID=" ${apnsEnvFile}''
      ++ [ ''! ${pkgs.gnugrep}/bin/grep -q "CHANGE_ME" ${apnsEnvFile}'' ]
    )
    ++ lib.optionals cfg.fcm.enable [
      "[ -s ${fcmServiceAccountFile} ]"
      ''! ${pkgs.gnugrep}/bin/grep -q "CHANGE_ME" ${fcmServiceAccountFile}''
    ];

  sygnalExecCondition = pkgs.writeShellScript "sygnal-exec-condition" ''
    ${lib.concatStringsSep " \\\n  && " execConditionChecks}
  '';
in
{
  options.services.mindroom-sygnal = {
    enable = mkEnableOption "MindRoom Sygnal Matrix push gateway";
    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "mindroom-sygnal.packages.\${pkgs.stdenv.hostPlatform.system}.default";
      description = "Package providing the MindRoom Sygnal pushkin add-on modules.";
    };
    image =
      mkOpt types.str
        "docker.io/matrixdotorg/sygnal@sha256:141f5e72fdf99a28e29ca87f3a46c19ed1cf21d5074aa3ef9df7c067e0c1c46a"
        "Pinned Sygnal v0.17.0 OCI image reference.";
    listenAddress = mkOpt types.str "127.0.0.1" "Host address to bind Sygnal's HTTP port on.";
    port = mkOpt types.port 5000 "Host port forwarded to Sygnal's container port 5000.";
    logLevel = mkOpt types.str "INFO" "Python log level for Sygnal root and access loggers.";
    xForwardedFor = mkOpt types.bool true "Whether Sygnal logs the X-Forwarded-For client address.";
    extraRestartTriggers =
      mkOpt (types.listOf pathOrStr) [ ]
        "Additional restartTriggers for sygnal-setup and podman-sygnal (e.g., agenix .age files).";

    apns = {
      enable = mkEnableOption "APNs push notifications via SafeApnsPushkin";
      appId = mkReqOpt types.str "Matrix push app_id for iOS APNs notifications.";
      keyFile = mkReqOpt pathOrStr "Runtime path to the APNs .p8 private key file.";
      envFile =
        mkOpt (types.nullOr pathOrStr) null
          "Optional env file sourced before envsubst on sygnal.yaml.";
      keyId = mkOpt types.str "$SYGNAL_APNS_KEY_ID" "APNs Key ID (or envsubst placeholder).";
      teamId = mkOpt types.str "$SYGNAL_APNS_TEAM_ID" "Apple Team ID (or envsubst placeholder).";
      topic = mkOpt types.str "$SYGNAL_APNS_TOPIC" "APNs topic / bundle ID (or envsubst placeholder).";
      platform =
        mkOpt types.str "$SYGNAL_APNS_PLATFORM"
          "APNs platform: production or sandbox (or envsubst placeholder).";
      convertDeviceTokenToHex =
        mkOpt types.bool false
          "Whether to convert APNs device tokens to hexadecimal.";
    };

    fcm = {
      enable = mkEnableOption "FCM HTTP v1 push notifications via SafeFcmPushkin";
      appId = mkReqOpt types.str "Matrix push app_id for Android FCM notifications.";
      projectId = mkReqOpt types.str "Firebase project ID for FCM HTTP v1.";
      serviceAccountFile = mkReqOpt pathOrStr "Runtime path to the Firebase service-account JSON file.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = config.virtualisation.oci-containers.backend == "podman";
        message = ''services.mindroom-sygnal requires virtualisation.oci-containers.backend = "podman".'';
      }
      {
        assertion = cfg.apns.enable || cfg.fcm.enable;
        message = "services.mindroom-sygnal: at least one of apns.enable or fcm.enable must be true.";
      }
      {
        assertion = !(cfg.apns.enable && cfg.fcm.enable) || cfg.apns.appId != cfg.fcm.appId;
        message = "services.mindroom-sygnal: apns.appId and fcm.appId must be distinct when both APNs and FCM are enabled.";
      }
      {
        assertion =
          !cfg.apns.enable
          || (
            cfg.apns.appId != ""
            && toString cfg.apns.keyFile != ""
            && (!useApnsEnvFile || toString cfg.apns.envFile != "")
          );
        message = "services.mindroom-sygnal.apns: appId, keyFile, and (when set) envFile must not be empty.";
      }
      {
        assertion =
          !cfg.fcm.enable
          || (cfg.fcm.appId != "" && cfg.fcm.projectId != "" && toString cfg.fcm.serviceAccountFile != "");
        message = "services.mindroom-sygnal.fcm: appId, projectId, and serviceAccountFile must not be empty.";
      }
      {
        assertion =
          !(cfg.apns.enable && cfg.apns.envFile == null)
          || lib.all (v: v != "" && !(lib.hasPrefix "$" v)) [
            cfg.apns.keyId
            cfg.apns.teamId
            cfg.apns.topic
            cfg.apns.platform
          ];
        message = "services.mindroom-sygnal.apns: set envFile or provide literal values for keyId, teamId, topic, and platform.";
      }
    ];

    virtualisation.podman.enable = true;

    systemd.services.sygnal-setup = {
      description = "Prepare Sygnal runtime configuration";
      before = [ "podman-sygnal.service" ];
      restartTriggers = sygnalRestartTriggers;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        RuntimeDirectory = "mindroom-sygnal";
        RuntimeDirectoryMode = "0750";
      };
      script = ''
        install -d -m 0750 /run/mindroom-sygnal
        rm -f /run/mindroom-sygnal/mindroom_*.py /run/sygnal/mindroom_*.py
        ${
          if cfg.apns.enable then
            "install -m 0400 ${apnsKeyFile} /run/mindroom-sygnal/sygnal-apns-key.p8"
          else
            "rm -f /run/mindroom-sygnal/sygnal-apns-key.p8"
        }
        ${
          if cfg.fcm.enable then
            "install -m 0400 ${fcmServiceAccountFile} /run/mindroom-sygnal/sygnal-fcm-service-account.json"
          else
            "rm -f /run/mindroom-sygnal/sygnal-fcm-service-account.json"
        }
        ${
          if useApnsEnvFile then
            ''
              (
                umask 077
                set -a
                . ${apnsEnvFile}
                set +a
                rm -f /run/mindroom-sygnal/sygnal.yaml
                ${pkgs.envsubst}/bin/envsubst -o /run/mindroom-sygnal/sygnal.yaml -i ${sygnalConfigTemplate}
              )
              chmod 0400 /run/mindroom-sygnal/sygnal.yaml''
          else
            "install -m 0400 ${sygnalConfigTemplate} /run/mindroom-sygnal/sygnal.yaml"
        }
      '';
    };

    virtualisation.oci-containers.containers.sygnal = {
      image = cfg.image;
      autoStart = true;
      environment = {
        PYTHONUNBUFFERED = "1";
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONPATH = "/sygnal-addons";
        SYGNAL_CONF = "/sygnal/sygnal.yaml";
      };
      ports = [ "${formattedListenAddress}:${toString cfg.port}:5000" ];
      volumes = [
        "/run/mindroom-sygnal:/sygnal:ro"
        "${cfg.package}:/sygnal-addons:ro"
      ];
    };

    systemd.services.podman-sygnal = {
      after = [
        "network-online.target"
        "sygnal-setup.service"
      ];
      requires = [ "sygnal-setup.service" ];
      wants = [ "network-online.target" ];
      restartTriggers = sygnalRestartTriggers;
      serviceConfig.ExecCondition = "${sygnalExecCondition}";
    };

    environment.systemPackages = [ pkgs.podman ];
  };
}
