/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  // GitHub Pages project sites (username.github.io/REPO/) serve everything
  // under a /REPO subpath. Set BASE_PATH at build time (GitHub Actions env)
  // to "/your-repo-name" for a project page, or leave unset for a user/org
  // page (username.github.io) or a custom domain.
  basePath: process.env.BASE_PATH || '',
  typescript: {
    ignoreBuildErrors: true,
  },
}

export default nextConfig
