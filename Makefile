.PHONY: css build up down restart logs ps

# Build Tailwind CSS locally (requires tailwindcss CLI)
css:
	tailwindcss -i sidecar/static/input.css -o sidecar/static/tailwind.css

# Build Tailwind CSS minified
css-min:
	tailwindcss -i sidecar/static/input.css -o sidecar/static/tailwind.css --minify

# Watch for changes and rebuild Tailwind CSS
css-watch:
	tailwindcss -i sidecar/static/input.css -o sidecar/static/tailwind.css --watch

# Build the sidecar Docker image
build:
	podman-compose build sidecar

# Build with no cache
build-clean:
	podman-compose build --no-cache sidecar

# Start all services
up:
	podman-compose up -d

# Stop all services
down:
	podman-compose down

# Restart sidecar (picks up volume-mounted changes)
restart:
	podman-compose restart sidecar

# Rebuild CSS and restart sidecar
deploy: css
	podman-compose restart sidecar

# View sidecar logs
logs:
	podman-compose logs -f sidecar

# Show running services
ps:
	podman-compose ps
