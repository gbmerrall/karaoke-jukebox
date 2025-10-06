#!/bin/bash
# Quick setup script for development

echo "🎤 Karaoke Jukebox - Quick Setup"
echo ""

# Copy .env.example if .env doesn't exist
if [ ! -f .env ]; then
    echo "📝 Creating .env from .env.example..."
    cp .env.example .env

    # Generate a secret key
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

    # Update .env with generated secret key
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        sed -i '' "s/your_secret_key_here/$SECRET_KEY/" .env
    else
        # Linux
        sed -i "s/your_secret_key_here/$SECRET_KEY/" .env
    fi

    echo "✅ Created .env file with generated SECRET_KEY"
    echo ""
    echo "⚠️  IMPORTANT: You still need to set these values in .env:"
    echo "   - ADMIN_PASSWORD (choose a secure password)"
    echo "   - YOUTUBE_API_KEY (get from https://console.cloud.google.com/apis/credentials)"
    echo ""
    echo "Edit .env now? (y/n)"
    read -r response
    if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        ${EDITOR:-nano} .env
    fi
else
    echo "✅ .env file already exists"
fi

echo ""
echo "🚀 Setup complete! Run the server with:"
echo "   ./run.sh     (or)    python run.py"
echo ""
