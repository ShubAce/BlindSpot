import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Caddy compresses every response in front of `next start` (infra/aws/Caddyfile), and Vercel's
  // edge does the same there, so Node does not spend its small heap and CPU gzipping as well.
  compress: false,
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
