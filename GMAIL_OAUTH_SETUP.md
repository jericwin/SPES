# Gmail OAuth Setup Guide

This guide will help you set up Gmail OAuth authentication for the SPES application system.

## Prerequisites

1. A Google Cloud Platform account
2. Access to Google Cloud Console

## Step 1: Create Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the Google+ API (if not already enabled)

## Step 2: Configure OAuth Consent Screen

1. In the Google Cloud Console, go to "APIs & Services" > "OAuth consent screen"
2. Choose "External" user type (unless you have a Google Workspace account)
3. Fill in the required information:
   - App name: "SPES Application System"
   - User support email: Your email
   - Developer contact information: Your email
4. Add scopes:
   - `email`
   - `profile`
   - `openid`
5. Add test users (your Gmail addresses) if in testing mode

## Step 3: Create OAuth 2.0 Credentials

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "OAuth 2.0 Client IDs"
3. Choose "Web application"
4. Add authorized JavaScript origins:
   - `http://localhost:5000` (for development)
   - `https://yourdomain.com` (for production)
5. Add authorized redirect URIs:
   - `http://localhost:5000` (for development)
   - `https://yourdomain.com` (for production)
6. Copy the Client ID

## Step 4: Configure Environment Variables

1. Set the following environment variables:

   ```bash
   export GOOGLE_CLIENT_ID="your-client-id-here"
   export GOOGLE_CLIENT_SECRET="your-client-secret-here"
   ```

2. Or create a `.env` file in your project root:
   ```
   GOOGLE_CLIENT_ID=your-client-id-here
   GOOGLE_CLIENT_SECRET=your-client-secret-here
   ```

## Step 5: Update Application Configuration

1. Replace `'your-google-client-id-here'` in `templates/home.html` with your actual Client ID
2. Update the `GOOGLE_CLIENT_ID` in `app.py` with your actual Client ID

## Step 6: Database Migration

Run the database migration to add the `id_number` field:

```sql
-- Run this SQL command in your MySQL database
ALTER TABLE users ADD COLUMN id_number VARCHAR(50) UNIQUE AFTER email;
CREATE INDEX idx_users_id_number ON users(id_number);
```

## Step 7: Install Dependencies

Install the required Python packages:

```bash
pip install -r requirements.txt
```

## Step 8: Test the Integration

1. Start your Flask application
2. Go to the login page
3. Click "Login with Gmail" or "Register with Gmail"
4. Select your Gmail account
5. Verify that you're logged in and redirected to the appropriate dashboard

## Features

### Automatic ID Number Generation

- Each Gmail account gets a unique ID number
- Format: `SPES{year}{8-character-hash}`
- Example: `SPES2025A1B2C3D4`

### User Experience

- **New Applicants**: Can register/login with Gmail
- **Old Applicants**: Can register/login with Gmail
- **Automatic Account Creation**: First-time users are automatically registered
- **Seamless Login**: Returning users are automatically logged in

### Security

- Google OAuth 2.0 token verification
- Gmail-only restriction (must use @gmail.com addresses)
- Secure session management
- Automatic ID number generation from email hash

## Troubleshooting

### Common Issues

1. **"Invalid token" error**

   - Check that your Client ID is correct
   - Verify that the domain is authorized in Google Cloud Console

2. **"Please use a Gmail account" error**

   - Only @gmail.com addresses are accepted
   - Other Google accounts (@company.com) are not supported

3. **Database errors**

   - Ensure the `id_number` column exists in the users table
   - Check database connection

4. **JavaScript errors**
   - Verify that the Google OAuth script is loading
   - Check browser console for errors

### Testing

1. Use test Gmail accounts during development
2. Add test users in Google Cloud Console OAuth consent screen
3. Test both new and old applicant flows

## Production Deployment

1. Update authorized origins and redirect URIs in Google Cloud Console
2. Set production environment variables
3. Update Client ID in production code
4. Test thoroughly before going live

## Support

For issues with Google OAuth setup, refer to:

- [Google OAuth 2.0 Documentation](https://developers.google.com/identity/protocols/oauth2)
- [Google Cloud Console Help](https://cloud.google.com/docs)
