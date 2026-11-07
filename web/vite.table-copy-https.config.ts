import { readFileSync } from "node:fs";
import { defineConfig, mergeConfig } from "vite";
import base from "./vite.config";

// Isolated HTTPS experiment. Supply a local certificate; no host trust changes.
export default defineConfig((config) => {
  const { TABLE_COPY_CERT, TABLE_COPY_KEY } = process.env;
  if (!TABLE_COPY_CERT || !TABLE_COPY_KEY) {
    throw new Error("Set TABLE_COPY_CERT and TABLE_COPY_KEY for the HTTPS experiment");
  }
  return mergeConfig(base(config), {
    server: {
      host: "127.0.0.1",
      https: {
        cert: readFileSync(TABLE_COPY_CERT),
        key: readFileSync(TABLE_COPY_KEY),
      },
    },
  });
});
