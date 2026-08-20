#include <mach-o/dyld.h>
#include <libgen.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char executable[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0) {
        fprintf(stderr, "MLXBarCoordinator: executable path is too long\n");
        return 1;
    }
    char resolved[PATH_MAX];
    if (realpath(executable, resolved) == NULL) {
        perror("MLXBarCoordinator");
        return 1;
    }
    char directory[PATH_MAX];
    strlcpy(directory, resolved, sizeof(directory));
    char target[PATH_MAX];
    snprintf(target, sizeof(target), "%s/../Resources/coordinator/MLXBarCoordinator", dirname(directory));
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    argv[0] = target;
    execv(target, argv);
    perror("MLXBarCoordinator");
    return 1;
}
