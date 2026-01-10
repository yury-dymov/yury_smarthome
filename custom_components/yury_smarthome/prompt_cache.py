import aiofiles


class PromptCache:
    cache: dict[str, str] = {}

    async def get(self, key: str) -> str:
        cached_version = self.cache.get(key)
        if cached_version is not None:
            return cached_version
        async with aiofiles.open(key) as file:
            data = await file.read()
            self.cache[key] = data
            return data
