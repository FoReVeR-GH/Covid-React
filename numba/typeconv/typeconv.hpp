#ifndef NUMBA_TYPECONV_HPP_
#define NUMBA_TYPECONV_HPP_
#include <string>
#include <vector>

/*
This object must be int sized
*/
class Type{
public:
    Type();
    Type(int id);
    Type(const Type& other);
    Type& operator = (const Type& other);
    bool valid() const;
    bool operator ==(const Type& other) const;
    bool operator !=(const Type& other) const;
    bool operator <(const Type& other) const;

    int get() const;

private:
    int id;
};

enum TypeCompatibleCode{
    // No match
    TCC_FALSE = 0,
    // Exact match
    TCC_EXACT,
    // Subtype is UNUSED
    TCC_SUBTYPE,
    // Promotion with no precision loss
    TCC_PROMOTE,
    // Conversion with no precision loss
    // e.g. int32 to double
    TCC_CONVERT_SAFE,
    // Conversion with precision loss
    // e.g. int64 to double (53 bits precision)
    TCC_CONVERT_UNSAFE,
};

typedef std::pair<Type, Type> TypePair;
//typedef std::map<TypePair, TypeCompatibleCode> TCCMap;

struct TCCRecord {
    TypePair key;
    TypeCompatibleCode val;
};

typedef std::vector<TCCRecord> TCCMapBin;

enum {TCCMAP_SIZE = 512};

class TCCMap {
public:
    unsigned int hash(const TypePair &key) const;
    void insert(const TypePair &key, TypeCompatibleCode val);
    TypeCompatibleCode find(const TypePair &key) const;
private:
    TCCMapBin records[TCCMAP_SIZE];
};

struct Rating{
    unsigned int promote;
    unsigned int safe_convert;
    unsigned int unsafe_convert;

    Rating();
    void bad();

    bool operator < (const Rating &other) const;
    bool operator == (const Rating &other) const;
};


class TypeManager{
public:
    bool canPromote(Type from, Type to) const;
    bool canUnsafeConvert(Type from, Type to) const;
    bool canSafeConvert(Type from, Type to) const;

    void addPromotion(Type from, Type to);
    void addUnsafeConversion(Type from, Type to);
    void addSafeConversion(Type from, Type to);
    void addCompatibility(Type from, Type to, TypeCompatibleCode by);

    TypeCompatibleCode isCompatible(const Type &from, const Type &to) const;

    /**
    Output stored in selected.
    Returns
        Number of matches
    */
    int selectOverload(const Type sig[], const Type ovsigs[], int &selected,
                       int sigsz, int ovct, bool allow_unsafe) const;

private:
    int _selectOverload(const Type sig[], const Type ovsigs[], int &selected,
                        int sigsz, int ovct, bool allow_unsafe,
                        Rating ratings[], int candidates[]) const;

    TCCMap tccmap;
};


const char* TCCString(TypeCompatibleCode tcc);


#endif // NUMBA_TYPECONV_HPP_
