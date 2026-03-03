#!/bin/bash

echo "--------------------------------------------------------"
echo "🌪️ INSTALLING ALL 15 LANGUAGES FOR JUDGE VORTEX..."
echo "--------------------------------------------------------"

# 1. C, C++, and Swift (Apple Command Line Tools)
echo "Installing Apple Native Compilers (C, C++, Swift)..."
xcode-select --install 2>/dev/null || echo "Apple Command Line Tools already installed."

# 2. Homebrew (The Mac Package Manager)
# if ! command -v brew &> /dev/null; then
#     echo "Installing Homebrew..."
#     /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# else
#     echo "Homebrew found. Updating..."
#     brew update
# fi

# 3. Install the Exact Versions Needed
echo "Downloading core languages via Homebrew (This will take a few minutes)..."
brew install python@3.11
brew install node@18
brew install openjdk@17x
brew install ruby@3.2
brew install php@8.2
brew install go@1.21
brew install rust
brew install ghc           # Haskell compiler
brew install scala         # Scala compiler
brew install sqlite        # SQL Database engine
brew install --cask dotnet-sdk # C# (.NET 8.0)

# 4. TypeScript (Requires Node/NPM to be installed first)
echo "Installing TypeScript globally..."
/opt/homebrew/opt/node@18/bin/npm install -g typescript 2>/dev/null || npm install -g typescript

echo "--------------------------------------------------------"
echo "✅ ALL COMPILERS AND RUNTIMES INSTALLED!"
echo "--------------------------------------------------------"