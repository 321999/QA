# ============================================
# Stage 1: Build React frontend
# ============================================
FROM node:20-alpine AS frontend-build

WORKDIR /frontend

# Copy package files first for Docker layer caching
COPY frontend/package*.json ./

RUN npm ci

# Copy frontend source
COPY frontend/ ./

# Build React application
RUN npm run build


# ============================================
# Stage 2: Python + FastAPI application
# ============================================
FROM python:3.12-slim

WORKDIR /app

# Prevent Python from creating .pyc files
# and make logs appear immediately
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install Python dependencies
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