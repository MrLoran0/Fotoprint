FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY . .
# в контейнере фронтенд всегда работает с настоящим API через nginx (/api → api:8000)
ENV VITE_API_BASE=/api
# только для тестового стенда: быстрый вход под тестовыми ролями
ARG VITE_DEMO_PASSWORD
ENV VITE_DEMO_PASSWORD=$VITE_DEMO_PASSWORD
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
