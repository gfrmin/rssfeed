FROM julia:1.11-bookworm

WORKDIR /app

# Copy project files first for Docker layer caching
COPY Project.toml Manifest.toml ./

# Install exact pinned versions and precompile at build time
RUN julia --project=. -e 'using Pkg; Pkg.instantiate(); Pkg.precompile()'

EXPOSE 8081

CMD ["julia", "--project=.", "domains/rss/server.jl"]
