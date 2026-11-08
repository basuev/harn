/* Synthetic connections for core tests. No service adapters are needed. */
#ifdef CONFIG_CONNECTIONS
#undef CONFIG_CONNECTIONS
#define FIXTURE_CONNECTIONS
#endif
#include "../config.def.h"
#undef DEFAULT_PROVIDER
#define DEFAULT_PROVIDER "fixture"
#undef DEFAULT_EFFORT
#define DEFAULT_EFFORT "medium"
#ifdef FIXTURE_CONNECTIONS
#include "fixture.h"
static const Connection connections[] = {
    {"fixture", "http://fixture.invalid/v1", "HARN_FIXTURE_KEY", "fixture-model", &fixture_provider},
    {"fixture-key", "http://second.fixture.invalid/v1", "HARN_SECOND_KEY",
     "fixture/other-model", &fixture_provider},
    {0},
};
#endif
