/*
 * This C file is compiled and linked into pycc-generated shared objects.
 * It provides the Numba helper functions for runtime use in pycc-compiled
 * functions.
 */

#include <Python.h>

#define NUMBA_EXPORT_FUNC(_rettype) _rettype
#define NUMBA_EXPORT_DATA(_vartype) _vartype

#include "../_helperlib.c"

/* NOTE: import_array() is macro, not a function.  It returns NULL on
   failure on py3, but nothing on py2. */
#if PY_MAJOR_VERSION >= 3
    static void *
    wrap_import_array(void) {
        import_array();
        return (void *) 1;
    }
#else
    static void
    wrap_import_array(void) {
        import_array();
    }
#endif


static int
init_numpy(void) {
    #if PY_MAJOR_VERSION >= 3
        return wrap_import_array() != NULL;
    #else
        wrap_import_array();
        return 1;   /* always succeed */
    #endif
}

#ifndef PYCC_INIT_FUNCTION
#error PYCC_INIT_FUNCTION must be defined
#endif

void
PYCC_INIT_FUNCTION(PyObject *module)
{
    if (!init_numpy())
        Py_FatalError("Failed initializing numpy C API");
}
