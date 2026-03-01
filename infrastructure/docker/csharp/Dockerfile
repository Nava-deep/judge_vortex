FROM mcr.microsoft.com/dotnet/sdk:7.0

WORKDIR /app
# Create a dummy project to cache all .NET internals
RUN dotnet new console -n Vortex
WORKDIR /app/Vortex

# Pre-build it once so all libraries are 'Warmed'
RUN dotnet build -c Release

# This folder is where the user's 'Program.cs' will be mounted
ENTRYPOINT ["/bin/bash", "-c"]