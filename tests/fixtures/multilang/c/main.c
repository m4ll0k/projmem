#include <stdio.h>
#include "util.h"

int compute(int x) {
    return x + 1;
}

int main(void) {
    printf("%d\n", compute(helper()));
    return 0;
}
