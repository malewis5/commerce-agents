// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ["web-shared"],
  // A deployment serves both surfaces of one example from one origin: the storefront at
  // the root, the portal under /portal (examples/entertainment/vercel.json routes them).
  // The prefix has to be built in, since it is what the portal's own asset and link URLs
  // carry. Locally each surface has a port of its own and there is no prefix.
  basePath: process.env.VERCEL ? "/portal" : undefined,
};

export default nextConfig;
