# Pushing to GitHub & Creating a HACS Integration

This guide shows how to push the repository to GitHub and set it up as a HACS custom integration.

## Step 1: Create a GitHub Repository

1. Go to [github.com](https://github.com) and sign in to your account
2. Click the **"+"** icon in the top right and select **"New repository"**
3. Set the repository name to: **`apstorage-ble`**
4. Set the description to: **"Home Assistant integration for APstorage ELT-12 PCS via BLE"**
5. Choose **Public** (required for HACS to discover it)
6. DO NOT initialize with README, .gitignore, or license (we already have these)
7. Click **"Create repository"**

## Step 2: Add Remote and Push

After creating the repository, GitHub will show you commands. Replace `YOUR_USERNAME` with your GitHub username:

```bash
cd /home/per/vscode/apstorage-ble

# Add the remote (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/apstorage-ble.git

# Rename branch to main (HACS prefers 'main')
git branch -M main

# Push commits and tags
git push -u origin main
git push origin v0.0.1
```

## Step 3: Verify on GitHub

Visit `https://github.com/YOUR_USERNAME/apstorage-ble` and confirm:
- ✅ Files are visible (README.md, LICENSE, custom_components/, etc.)
- ✅ Tag v0.0.1 exists (shown under "Releases")
- ✅ Repository is public

## Step 4: Register with HACS

The repository is now ready for HACS. Users can install it by:

### In Home Assistant:
1. Open **HACS** (you must install HACS first if you don't have it)
2. Go to **"Custom repositories"**
3. Add: `https://github.com/YOUR_USERNAME/apstorage-ble`
4. Category: **"Integration"**
5. Click **"Create"** and wait for indexing
6. Find **"APstorage BLE"** in HACS and click **"Install"**
7. Restart Home Assistant

### Automatic HACS Discovery (Optional):
To have HACS automatically discover your repository, submit it to the official HACS repository list:
1. Fork: https://github.com/hacs/default
2. Add your repo info to `manifest.json` in the integrations folder
3. Submit a Pull Request

For first-time setup, users can follow the manual path above.

## Troubleshooting

**Commit already exists locally?**
If git refuses to push, use:
```bash
git push -f origin main
```

**Need to update the integration?**
After making changes:
```bash
cd /home/per/vscode/apstorage-ble
git add .
git commit -m "Description of changes"
git push origin main
```

**Create a new release tag:**
```bash
git tag -a v0.0.2 -m "Release v0.0.2: description"
git push origin v0.0.2
```

---

**Note:** After the first push, HACS typically requires 24-48 hours to index your repository. You can speed this up by submitting to the official HACS repository list.
