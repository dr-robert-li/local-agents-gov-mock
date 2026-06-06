FROM node:20-alpine

WORKDIR /app

# wget (used by the compose healthcheck) ships with the alpine base image.
COPY package.json ./
RUN npm install --omit=dev

COPY . .

EXPOSE 3000
CMD ["node", "server.js"]
