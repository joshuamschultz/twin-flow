// Starts a Vite dev server for the fixtures pages, so component tests need no build.
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { createServer, type ViteDevServer } from "vite";

const web = fileURLToPath(new URL("../../../", import.meta.url));

export async function startFixtures(): Promise<{
  server: ViteDevServer;
  url: string;
}> {
  const server = await createServer({
    root: web,
    configFile: false,
    plugins: [react()],
    logLevel: "error",
    server: { port: 0, strictPort: false, fs: { allow: [web + ".."] } },
  });
  await server.listen();
  const url = server.resolvedUrls!.local[0].replace(/\/$/, "");
  return { server, url };
}
