/* Adapted from CPython3.7 Include/listobject.h */

#ifndef NUMBA_LIST_H
#define NUMBA_LIST_H

typedef int (*list_item_comparator_t)(const char *lhs, const char *rhs);
typedef void (*list_refcount_op_t)(const void*);

typedef struct {
    list_item_comparator_t    item_equal;
    list_refcount_op_t       item_incref;
    list_refcount_op_t       item_decref;
} list_type_based_methods_table;

typedef struct {
    /* Size of the list.  */
    Py_ssize_t      size;
    /* Size of the list items. */
    Py_ssize_t      item_size;

    /* items contains space for 'allocated' elements.  The number
     * currently in use is size.
     * Invariants:
     *     0 <= size <= allocated
     *     len(list) == size
     *     item == NULL implies size == allocated == 0
     * FIXME: list.sort() temporarily sets allocated to -1 to detect mutations.
     *
     * Items must normally not be NULL, except during construction when
     * the list is not yet visible outside the function that builds it.
     */
    Py_ssize_t allocated;

    /* Method table for type-dependent operations. */
    list_type_based_methods_table methods;

    /* Array/pointer for items. Interpretation is governed by item_size. */
    char  * items;
} NB_List;


typedef struct {
    /* parent list */
    NB_List         *parent;
    /* list size */
    Py_ssize_t       size;
    /* iterator position; indicates the next position to read */
    Py_ssize_t       pos;
} NB_ListIter;

NUMBA_EXPORT_FUNC(void)
numba_list_set_method_table(NB_List *lp, list_type_based_methods_table *methods);

NUMBA_EXPORT_FUNC(int)
numba_list_new(NB_List **out, Py_ssize_t item_size, Py_ssize_t allocated);

NUMBA_EXPORT_FUNC(void)
numba_list_free(NB_List *lp);

NUMBA_EXPORT_FUNC(Py_ssize_t)
numba_list_length(NB_List *lp);

NUMBA_EXPORT_FUNC(int)
numba_list_setitem(NB_List *lp, Py_ssize_t index, const char *item);

NUMBA_EXPORT_FUNC(int)
numba_list_getitem(NB_List *lp, Py_ssize_t index, char *out);

NUMBA_EXPORT_FUNC(int)
numba_list_append(NB_List *lp, const char *item);

NUMBA_EXPORT_FUNC(int)
numba_list_pop(NB_List *lp, Py_ssize_t index, char *out);

// FIXME: should this be public?
NUMBA_EXPORT_FUNC(int)
numba_list_resize(NB_List *lp, Py_ssize_t newsize);

NUMBA_EXPORT_FUNC(size_t)
numba_list_iter_sizeof(void);

NUMBA_EXPORT_FUNC(void)
numba_list_iter(NB_ListIter *it, NB_List *l);

NUMBA_EXPORT_FUNC(int)
numba_list_iter_next(NB_ListIter *it, const char **item_ptr);

#endif
