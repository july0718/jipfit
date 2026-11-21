FROM node:22-alpine AS build
WORKDIR /web
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY Coordinates.jsx geocode-utils.cjs app.jsx staticData.js DistrictMap.jsx Account.jsx PriceCharts.jsx map-unverified-complexes.json ./
RUN npm run build

FROM nginx:stable-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html style.css config.js /usr/share/nginx/html/
COPY --from=build /web/dist/app.js /usr/share/nginx/html/app.js
COPY public/ /usr/share/nginx/html/


