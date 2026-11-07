/* Isolated fixture configuration, never used by release builds. */
#ifdef CONFIG_CONNECTIONS
#undef CONFIG_CONNECTIONS
#define FIXTURE_CONNECTIONS
#endif
#include "../config.def.h"
#undef DEFAULT_EFFORT
#define DEFAULT_EFFORT "medium"
#ifdef FIXTURE_CONNECTIONS
static const Connection connections[] = {
    {"chatgpt", "https://api.openai.com/v1", "", "gpt-6.1-sol"},
    /* Historical identities exercise compatibility with saved sessions. */
    {"allmodels", "http://fixture.invalid/v1", "ALLMODELS_API_KEY",
     "deepseek/deepseek-v4.1-flash-abliterated"},
};
#endif
