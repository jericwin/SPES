# Environment Configuration Setup

## Step 1: Create .env File

1. Copy the `config_template.env` file to `.env` in your project root:

   ```bash
   cp config_template.env .env
   ```

2. Or create a new `.env` file manually in your project root directory.

## Step 2: Add Your Google OAuth Credentials

Edit the `.env` file and replace the placeholder values with your actual credentials:

```env
# Google OAuth Configuration
GOOGLE_CLIENT_ID=your-actual-google-client-id-here
GOOGLE_CLIENT_SECRET=your-actual-google-client-secret-here

# Database Configuration (if needed)
DB_HOST=localhost
DB_USER=your-db-username
DB_PASSWORD=your-db-password
DB_NAME=your-db-name

# Flask Configuration
FLASK_SECRET_KEY=your-secret-key-here
FLASK_ENV=development
```

## Step 3: Update JavaScript Configuration

In `templates/home.html`, replace the placeholder Client ID:

```javascript
// Find this line in templates/home.html
const GOOGLE_CLIENT_ID = "your-google-client-id-here";

// Replace with your actual Client ID
const GOOGLE_CLIENT_ID = "your-actual-google-client-id-here";
```

## Step 4: Install Dependencies

Install the required packages:

```bash
pip install -r requirements.txt
```

## Step 5: Test Configuration

1. Start your Flask application
2. Go to the login page
3. Try the Gmail login functionality
4. Check the console for any errors

## Security Notes

- **Never commit .env files to version control**
- Keep your Google OAuth credentials secure
- Use different credentials for development and production
- The .env file should be in your .gitignore

## Example .env File

Here's what your `.env` file should look like with real credentials:

```env
# Google OAuth Configuration
GOOGLE_CLIENT_ID=123456789-abcdefghijklmnop.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-abcdefghijklmnopqrstuvwxyz

# Database Configuration
DB_HOST=localhost
DB_USER=root
DB_PASSWORD=yourpassword
DB_NAME=digispes

# Flask Configuration
FLASK_SECRET_KEY=your-super-secret-key-here
FLASK_ENV=development
```

## Troubleshooting

### Common Issues:

1. **"No module named 'dotenv'"**

   - Run: `pip install python-dotenv`

2. **Environment variables not loading**

   - Make sure `.env` file is in the project root
   - Check that `load_dotenv()` is called before using environment variables

3. **Google OAuth not working**

   - Verify your Client ID is correct in both `.env` and `home.html`
   - Check that your domain is authorized in Google Cloud Console

4. **Database connection issues**
   - Update database credentials in `.env` file
   - Ensure database server is running

## Production Deployment

For production:

1. Set environment variables on your server
2. Use a secure secret key
3. Update authorized domains in Google Cloud Console
4. Use production database credentials
