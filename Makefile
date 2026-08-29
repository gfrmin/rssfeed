.PHONY: css build up down restart logs ps test lint fmt

# Run the test suite (must go through uv — system Python lacks the deps)
test:
	cd sidecar && uv run pytest

# Lint
lint:
	cd sidecar && uv run ruff check .

# Format (see README: the repo is not yet fully ruff-formatted)
fmt:
	cd sidecar && uv run ruff format .

# Rebuild the Tailwind stylesheet. The OUTPUT IS COMMITTED -- running the app
# needs no Node toolchain -- so this is only for when input.css or a template's
# utility classes change. `npm install` pulls the pinned CLI into
# sidecar/node_modules (gitignored); the old target assumed a global `tailwindcss`
# binary that nothing installed, which is how static/tailwind.css came to be
# referenced by base.html for months without ever existing.
css:
	cd sidecar && npm install && npm run build:css

# Rebuild on change while working on the management pages
css-watch:
	cd sidecar && npm install && npm run watch:css

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
