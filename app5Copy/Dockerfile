# from better side even thoug i have already created the build still i will create a multi stage build to reduce the size of the image and also to make sure that the final image only contains the necessary files for running the application
FROM node:20-alpine AS frontend-build

WORKDIR /frontend

# Copy package files first for Docker layer caching
COPY frontend/package*.json ./

RUN npm ci

# Copy frontend src
COPY frontend/ ./

# Build React application
RUN npm run build


# ============================================
# Stage 2: Python + FastAPI application
# ============================================
FROM python:3.12

WORKDIR /app

# Prevent Python from creating .pyc files and make the logs will appear immediately 

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1


COPY backend/requirements.txt ./requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

# Copy backend application
COPY backend/ ./

# Copy React production build generated
# in Stage 1
COPY --from=frontend-build /frontend/dist /frontend/dist
# COPY --from=frontend-build /frontend/dist /frontend/dist
# Application port
EXPOSE 8001

# Start FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]