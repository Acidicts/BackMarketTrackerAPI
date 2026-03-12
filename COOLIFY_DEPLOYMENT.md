# Coolify Deployment Guide

## Problem
Coolify deployment was failing with DNS resolution error:
```
fatal: unable to access 'https://github.com/Acidicts/BackMarketTrackerAPI/': Could not resolve host: github.com
```

This occurred because Coolify's helper container couldn't resolve DNS when trying to clone the repository.

## Solution
Instead of having Coolify build from source (which requires git clone), we use pre-built Docker images:

1. **GitHub Actions** automatically builds Docker images on every push to `main`
2. Images are pushed to **GitHub Container Registry (GHCR)**
3. **Coolify** pulls the pre-built image instead of cloning the repo

## Setup Instructions

### Step 1: Enable GitHub Actions
The workflow file `.github/workflows/docker-build.yml` is already configured and will run automatically on pushes to `main`.

### Step 2: Make Images Public (Required)
1. Go to https://github.com/Acidicts/BackMarketTrackerAPI/pkgs/container/backmarkettrackerapi
2. Click "Package settings"
3. Scroll down to "Danger Zone"
4. Click "Change visibility" and set to **Public**

This is necessary so Coolify can pull the image without authentication.

### Step 3: Configure Coolify

#### Option A: Using Docker Image (Recommended)
1. In Coolify, create or edit your application
2. Select **"Docker Image"** as the Build Pack
3. Set the image to: `ghcr.io/acidicts/backmarkettrackerapi:latest`
4. Configure your environment variables if needed
5. Set port to `8000`
6. Deploy!

#### Option B: Using Docker Compose
1. In Coolify, select **"Docker Compose"** as the Build Pack
2. Use the `docker-compose.prod.yml` file from the repository
3. Deploy!

## Verification
After the first push to `main`:
1. Check GitHub Actions tab to ensure the workflow runs successfully
2. Verify the image is available at `ghcr.io/acidicts/backmarkettrackerapi:latest`
3. Deploy in Coolify using the pre-built image

## Additional Notes
- The workflow builds images for every push to `main` and tags them as `latest`
- For pull requests, it builds but doesn't tag as `latest`
- Images are also tagged with the commit SHA for version tracking
