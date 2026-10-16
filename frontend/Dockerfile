# Local development image (Vite dev server). The production image (static
# build served by nginx) is docker/frontend/Dockerfile.prod.
FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

EXPOSE 5180

CMD ["npm", "run", "dev"]
