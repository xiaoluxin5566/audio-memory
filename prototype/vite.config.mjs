import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const MUTATION_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const devPort = Number(process.env.AUDIO_MEMORY_DEV_PORT ?? "5173");
const expectedProfileOverride = process.env.AUDIO_MEMORY_EXPECTED_PROFILE_OVERRIDE;
const backend = new URL(
  process.env.AUDIO_MEMORY_BACKEND_URL ?? "http://127.0.0.1:8766",
);
const loopbackNames = new Set(["127.0.0.1", "localhost", "[::1]"]);

if (backend.protocol !== "http:" || !loopbackNames.has(backend.hostname)) {
  throw new Error("AUDIO_MEMORY_BACKEND_URL must be an HTTP loopback URL");
}

const devAuthorities = new Set([
  `127.0.0.1:${devPort}`,
  `localhost:${devPort}`,
]);

function protectAndRewriteMutation(req, res) {
  if (!MUTATION_METHODS.has(req.method)) return;
  const host = req.headers.host?.toLowerCase();
  const origin = req.headers.origin?.toLowerCase();
  if (!host || !devAuthorities.has(host) || origin !== `http://${host}`) {
    res.writeHead(403, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      detail: { code: "untrusted_dev_origin", message: "Trusted dev Origin required" },
    }));
    return req.url ?? "/api";
  }
  req.headers.origin = backend.origin;
}

export default defineConfig(({ command }) => ({
  define: {
    "import.meta.env.VITE_AUDIO_MEMORY_EXPECTED_PROFILE": JSON.stringify(
      command === "serve" ? (expectedProfileOverride ?? "development") : "",
    ),
  },
  build: {
    outDir: "dist/client",
  },
  optimizeDeps: {
    include: ["react", "react-dom/client"],
  },
  server: {
    host: "127.0.0.1",
    port: devPort,
    strictPort: true,
    allowedHosts: ["127.0.0.1", "localhost"],
    proxy: {
      "/api": {
        target: backend.origin,
        changeOrigin: true,
        bypass: protectAndRewriteMutation,
      },
    },
    warmup: {
      clientFiles: ["./src/main.jsx"],
    },
  },
  plugins: [react()],
}));
