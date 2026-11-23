FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
RUN npm ci

# Source code is mounted as a volume in development (see docker-compose.yml).
# COPY is here so the image also works standalone.
COPY . .

# Vite's default dev-server port
EXPOSE 5173

# --host 0.0.0.0 ensures the dev server is reachable from outside the container
CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "5173"]
