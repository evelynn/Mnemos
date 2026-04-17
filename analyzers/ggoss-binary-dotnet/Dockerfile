FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY src/ .
RUN dotnet publish ggoss-binary-dotnet.csproj -c Release -o /out --no-self-contained

FROM mcr.microsoft.com/dotnet/sdk:8.0
WORKDIR /app
COPY --from=build /out ./
RUN mkdir -p /work/.mnemos/out
WORKDIR /work
ENTRYPOINT ["dotnet", "/app/ggoss-binary-dotnet.dll"]
