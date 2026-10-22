import { Container, getContainer } from "@cloudflare/containers";

export class SignalForgeContainer extends Container {
  defaultPort = 8080;
  requiredPorts = [8080];
  sleepAfter = "10m";
  pingEndpoint = "localhost/api/health";
  enableInternet = true;

  constructor(ctx, env) {
    super(ctx, env);
    this.envVars = {
      HOST: "0.0.0.0",
      PORT: "8080",
      KIC_DATA_DIR: "/tmp/signalforge-data",
      KIC_LOG_LEVEL: "INFO",
      NVIDIA_API_KEY: env.NVIDIA_API_KEY || "",
      NVIDIA_BASE_URL: env.NVIDIA_BASE_URL || "https://integrate.api.nvidia.com/v1",
      NVIDIA_MODEL: env.NVIDIA_MODEL || "nvidia/nemotron-3-ultra-550b-a55b",
      NVIDIA_TIMEOUT_SECONDS: env.NVIDIA_TIMEOUT_SECONDS || "45",
      DART_API_KEY: env.DART_API_KEY || "",
      ECOS_API_KEY: env.ECOS_API_KEY || "",
      KOSIS_API_KEY: env.KOSIS_API_KEY || "",
    };
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) {
      return getContainer(env.SIGNALFORGE, "production").fetch(request);
    }
    return env.ASSETS.fetch(request);
  },
};
