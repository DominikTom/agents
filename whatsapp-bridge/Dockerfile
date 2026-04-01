FROM node:20-slim

WORKDIR /app

COPY package.json .
RUN npm install --production

COPY . .

RUN mkdir -p /app/auth_info && chmod 777 /app/auth_info

EXPOSE 3001

CMD ["node", "index.js"]
