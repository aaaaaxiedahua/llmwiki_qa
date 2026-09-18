import { defineConfig } from "wxt";
import packageJson from "./package.json";

function requiredEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} must be set before building the extension`);
  }
  return value;
}

function origin(name: string, value: string): string {
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error();
    return url.origin;
  } catch {
    throw new Error(`${name} must be an absolute HTTP(S) URL`);
  }
}

export default defineConfig({
  srcDir: "src",
  modules: ["@wxt-dev/module-react"],
  // This is a Chrome MV3 extension. WXT's multi-browser default target asks
  // the pinned esbuild version to downlevel modern Supabase Auth code using a
  // destructuring transform it no longer supports, which prevents packaging.
  vite: () => ({
    build: { target: "chrome109" },
  }),
  // Dev runner config:
  //   - Persistent profile so the Google/Supabase sign-in survives reloads
  //   - Opens a known testbed URL so we can verify the content script bootstraps
  //     against a real site (CSP, CORS, real DOM)
  //   - Uses your actual Chrome binary, in a separate profile dir, so this
  //     doesn't interfere with your normal browsing session
  runner: {
    binaries: {
      chrome: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    },
    chromiumProfile: "/tmp/llmwiki-ext-profile",
    keepProfileChanges: true,
    startUrls: ["https://example.com/"],
  },
  // WXT loads dotenv after importing this config file, so resolve environment
  // values lazily. Evaluating them at module load silently ignored .env.
  manifest: () => {
    const apiOrigin = origin(
      "VITE_API_BASE_URL",
      process.env.VITE_API_BASE_URL ?? "https://api.llmwiki.app",
    );
    const supabaseOrigin = origin(
      "VITE_SUPABASE_URL",
      requiredEnv("VITE_SUPABASE_URL"),
    );
    requiredEnv("VITE_SUPABASE_ANON_KEY");

    return {
      name: "LLM Wiki",
      description: "Save any web page or PDF to your LLM Wiki knowledge base",
      version: packageJson.version,
      minimum_chrome_version: "109",
      permissions: ["activeTab", "identity", "offscreen", "storage", "scripting"],
      // activeTab covers the page itself. Explicit host access is still needed
      // for cross-origin API and Supabase Auth requests from extension pages.
      host_permissions: [
        `${apiOrigin}/*`,
        `${supabaseOrigin}/*`,
        "http://localhost/*",
      ],
      icons: {
        16: "icon/16.png",
        32: "icon/32.png",
        48: "icon/48.png",
        96: "icon/96.png",
        128: "icon/128.png",
      },
      action: {
        default_icon: {
          16: "icon/16.png",
          32: "icon/32.png",
        },
      },
    };
  },
});
