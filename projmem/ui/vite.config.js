import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// Build into ./dist; the Python package ships dist/* via package_data.
// The daemon serves the bundle at "/" so pip-installed users never need Node.
export default defineConfig({
    plugins: [react()],
    base: "./",
    build: {
        outDir: "dist",
        emptyOutDir: true,
        sourcemap: false,
    },
    server: {
        // For `npm run dev` against a separately-running `projmem daemon`.
        proxy: {
            "/healthz": "http://127.0.0.1:7777",
            "/state": "http://127.0.0.1:7777",
            "/notes": "http://127.0.0.1:7777",
            "/critical": "http://127.0.0.1:7777",
            "/control": "http://127.0.0.1:7777",
            "/events": { target: "ws://127.0.0.1:7777", ws: true },
        },
    },
});
