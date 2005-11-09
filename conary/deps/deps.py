#
# Copyright (c) 2004-2005 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any waranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#

import copy
import re
from conary.lib import util

DEP_CLASS_ABI		= 0
DEP_CLASS_IS		= 1
DEP_CLASS_OLD_SONAME	= 2
DEP_CLASS_FILES		= 3
DEP_CLASS_TROVES	= 4
DEP_CLASS_USE		= 5
DEP_CLASS_SONAME	= 6
DEP_CLASS_USERINFO      = 7
DEP_CLASS_GROUPINFO     = 8
DEP_CLASS_CIL           = 9
DEP_CLASS_JAVA          = 10

DEP_CLASS_NO_FLAGS      = 0
DEP_CLASS_HAS_FLAGS     = 1
DEP_CLASS_OPT_FLAGS     = 2

FLAG_SENSE_UNSPECIFIED  = 0         # used FlavorScore indices
FLAG_SENSE_REQUIRED     = 1
FLAG_SENSE_PREFERRED    = 2
FLAG_SENSE_PREFERNOT    = 3
FLAG_SENSE_DISALLOWED   = 4

DEP_MERGE_TYPE_NORMAL         = 1    # conflicts are reported
DEP_MERGE_TYPE_OVERRIDE       = 2    # new data wins
DEP_MERGE_TYPE_PREFS          = 3    # like override, but !ssl beats out ~!ssl
DEP_MERGE_TYPE_DROP_CONFLICTS = 4    # conflicting flags are removed 

senseMap = { FLAG_SENSE_REQUIRED   : "",
             FLAG_SENSE_PREFERRED  : "~",
             FLAG_SENSE_PREFERNOT  : "~!",
             FLAG_SENSE_DISALLOWED : "!" }

senseReverseMap = {}
for key, val in senseMap.iteritems():
    senseReverseMap[val] = key

dependencyClasses = {}
dependencyClassesByName = {}

def _registerDepClass(classObj):
    global dependencyClasses
    classObj.compileRegexp()
    dependencyClasses[classObj.tag] = classObj
    dependencyClassesByName[classObj.tagName] = classObj

class BaseDependency(object):

    __slots__ = ( '__weakref__' )

    """
    Implements a single dependency. This is relative to a DependencyClass,
    which is part of a DependencySet. Dependency Sets can be frozen and
    thawed.

    These are hashable, directly comparable, and implement a satisfies()
    method.
    """

    def __hash__(self):
        raise NotImplementedError

    def __eq__(self, other):
        raise NotImplementedError

    def __str__(self):
        raise NotImplementedError

    def freeze(self):
        raise NotImplementedError

    def satisfies(self, required):
        raise NotImplementedError

    def mergeFlags(self, other):
        raise NotImplementedError

    def getName(self):
        raise NotImplementedError

    def getFlags(self):
        raise NotImplementedError

    def __init__(self):
        raise NotImplementedError

class Dependency(BaseDependency):

    __slots__ = ( 'name', 'flags' )

    def __hash__(self):
	val = hash(self.name)
	for flag in self.flags.iterkeys():
	    val ^= hash(flag)
	return val
	
    def __eq__(self, other):
	return other.name == self.name and other.flags == self.flags

    def __str__(self):
	if self.flags:
	    flags = self.flags.items()
	    flags.sort()
	    return "%s(%s)" % (self.name, 
                    " ".join([ "%s%s" % (senseMap[x[1]], x[0]) for x in flags]))
	else:
	    return self.name

    def freeze(self):
	if self.flags:
	    flags = [ (x.replace(':', '::'), y) for x, y in self.flags.items() ]
	    flags.sort()
	    return "%s:%s" % (self.name.replace(':', '::'), 
                ":".join([ "%s%s" % (senseMap[x[1]], x[0]) for x in flags]))
	else:
	    return self.name.replace(':', '::')

    def score(self, required):
        """
        Returns a flavor matching score. This dependency is considered
        the "system" and the other is the flavor of the trove. In terms
        of dependencies, this set "provides" and the other "requires".

        False is returned if the two dependencies conflict.
        """
	if self.name != required.name: 
            return False

        score = 0
	for (requiredFlag, requiredSense) in required.flags.iteritems():
            thisSense = self.flags.get(requiredFlag, FLAG_SENSE_UNSPECIFIED)
            thisScore = flavorScores[(thisSense, requiredSense)]
            if thisScore is None:
                return False
            score += thisScore

        return score

    def emptyDepsScore(self):
        """ 
        Like score where this trove is the "requires" and the other trove
        provides nothing.  If all the requires are negative, (!foo)
        this could return something other than False
        """
        score = 0
        if not self.flags:
            # if there are no flags associated with this dependency,
            # then missing the base dep has to be enough to disqualify this
            # flavor
            return False
	for (requiredFlag, requiredSense) in self.flags.iteritems():
            thisScore = flavorScores[(FLAG_SENSE_UNSPECIFIED, requiredSense)]
            if thisScore is None:
                return False
            score += thisScore
        return score

    def satisfies(self, required):
	"""
	Returns whether or not this dependency satisfies the argument
	(which is a requires).

	@type required: Dependency
	"""
        return self.score(required) is not False

    def toStrongFlavor(self):
        newFlags = self.flags.copy()
        for (flag, sense) in self.flags.iteritems():
            if sense == FLAG_SENSE_PREFERNOT:
                newFlags[flag] = FLAG_SENSE_DISALLOWED
            elif sense == FLAG_SENSE_PREFERRED:
                newFlags[flag] = FLAG_SENSE_REQUIRED
        return Dependency(self.name, newFlags)

    def intersection(self, other):
        intFlags = {}
        for (flag, sense) in other.flags.iteritems():
            if flag in self.flags and self.flags[flag] == sense:
                intFlags[flag] = sense
        if not intFlags:
            if self.flags != other.flags:
                return None
        return Dependency(self.name, intFlags)

    def __and__(self, other):
        return self.intersection(other)

    def difference(self, other):
        diffFlags = self.flags.copy()
        for flag, value in other.flags.iteritems():
            if flag in diffFlags and value == diffFlags[flag]:
                del diffFlags[flag]
        if not diffFlags:
            return None
        else:
            return Dependency(self.name, diffFlags)

    def __sub__(self, other):
        return self.difference(other)

    def mergeFlags(self, other, mergeType = DEP_MERGE_TYPE_NORMAL):
	"""
	Returns a new Dependency which merges the flags from the two
	existing dependencies. We don't want to merge in place as this
	Dependency could be shared between many objects (via a 
	DependencyGroup).  Always pick an strong flavor over a weak one:
        e.g. when merging a set of flags with a ~foo and !foo, 
        make the merged flavor !foo.  
	"""
	allFlags = self.flags.copy()
        for (flag, otherSense) in other.flags.iteritems():
            if mergeType == DEP_MERGE_TYPE_PREFS and allFlags.has_key(flag) \
                    and otherSense == FLAG_SENSE_PREFERNOT \
                    and allFlags[flag] == FLAG_SENSE_DISALLOWED:
                allFlags[flag] = FLAG_SENSE_DISALLOWED
                continue
            elif mergeType == DEP_MERGE_TYPE_OVERRIDE or \
                 mergeType == DEP_MERGE_TYPE_PREFS    or \
                        not allFlags.has_key(flag):
                allFlags[flag] = otherSense
                continue

            thisSense = allFlags[flag]

            if thisSense == otherSense:
                # same flag, same sense
                continue

            if ((thisSense == FLAG_SENSE_REQUIRED and 
                        otherSense == FLAG_SENSE_DISALLOWED) or
                (thisSense == FLAG_SENSE_DISALLOWED and
                        otherSense == FLAG_SENSE_REQUIRED)   or
                (thisSense == FLAG_SENSE_PREFERRED and 
                        otherSense == FLAG_SENSE_PREFERNOT) or
                (thisSense == FLAG_SENSE_PREFERNOT and
                        otherSense == FLAG_SENSE_PREFERRED)):
                if mergeType == DEP_MERGE_TYPE_DROP_CONFLICTS:
                    del allFlags[flag]
                    continue
                thisFlag = "%s%s" % (senseMap[thisSense], flag)
                otherFlag = "%s%s" % (senseMap[otherSense], flag)
                raise RuntimeError, ("Invalid flag combination in merge:"
                                     " %s and %s"  % (thisFlag, otherFlag))

            # know they aren't the same, and they are compatible
            if thisSense == FLAG_SENSE_REQUIRED or \
                    thisSense == FLAG_SENSE_DISALLOWED:
                continue
            elif otherSense == FLAG_SENSE_REQUIRED or \
                    otherSense == FLAG_SENSE_DISALLOWED:
                allFlags[flag] = otherSense
                continue

            # we shouldn't end up here
            assert(0)

        return Dependency(self.name, allFlags)

    def getName(self):
        return (self.name,)

    def getFlags(self):
        return (self.flags.items(),)

    def __init__(self, name, flags = []):
	self.name = name
	if type(flags) == dict:
	    self.flags = flags
	else:
	    self.flags = {}
	    for (flag, sense) in flags:
		self.flags[flag] = sense

class DependencyClass(object):

    __slots__ = ( 'tag', 'members', 'depFormat', 'flagFormat', 
                  'flags', 'allowParseDep')

    depFormat = 'WORD'
    flagFormat = 'WORD'
    flagOption = DEP_CLASS_NO_FLAGS
    allowParseDep = True

    @classmethod
    def compileRegexp(class_):
        """ Class method that takes the abstract information about the format
            of this dependency class and turns it into a regexp that will
            match dep strings that can be parsed into a dependency of this 
            class.
        """
        if not class_.allowParseDep:
            return

        d = dict(flagFormat=class_.flagFormat,
                 depFormat=class_.depFormat)

        # zero or more space-separated flags 
        flagFmt = '(?:\( *(%(flagFormat)s?(?: +%(flagFormat)s)*) *\))?' 
        # add ^ and $ to ensure we match the entire string passed in
        regexp = ('^ *(%(depFormat)s) *' + flagFmt + ' *$') % d
        # word is a slightly larger group of chars than ident - 
        # includes . and +, because those are used in paths and 
        # sonames.  May need to be larger some day, and probably 
        # could be more restrictive for some groups.  Should not contain
        # /, as that's used as a special char in many dep classes.
        regexp = regexp.replace('WORD', '(?:[.0-9A-Za-z_+-]+)')
        regexp = regexp.replace('IDENT', '(?:[0-9A-Za-z_-]+)')
        class_.regexpStr = regexp
        class_.regexp = re.compile(regexp)

    @classmethod
    def parseDep(class_, s):
        """ Parses a dependency string of this class and returns the
            result.  Raises a ParseError on failure.
        """
        if not class_.allowParseDep:
            raise ParseError, "Invalid dependency class %s" % class_.tagName

        match = class_.regexp.match(s)
        if match is None:
            raise ParseError, "Invalid %s dependency: '%s'" % (class_.tagName, 
                                                               s)

        depName, flagStr = match.groups() # a dep is <depName>[(<flagStr>)]
                                          # flagStr is None if () not 
                                          # in the depStr

        flags = [] 
        if class_.flags == DEP_CLASS_NO_FLAGS:
            if flagStr is not None: 
                # the dep string specified at least () -
                # not allowed when the dep has no flags
                raise ParseError, ("bad %s dependency '%s':"
                                   " flags not allowed" % (class_.tagName, s))
        elif flagStr: 
            flags = [ (x, FLAG_SENSE_REQUIRED) for x in flagStr.split()]
        elif class_.flags == DEP_CLASS_HAS_FLAGS:
            raise ParseError, ("bad %s dependency '%s':"
                               " flags required" % (class_.tagName, s))
        else:
            assert(class_.flags == DEP_CLASS_OPT_FLAGS)

        return Dependency(depName, flags)


    def addDep(self, dep, mergeType = DEP_MERGE_TYPE_NORMAL):
        assert(dep.__class__.__name__ == self.depClass.__name__)

	if self.members.has_key(dep.name):
	    # this is a little faster then doing all of the work when
	    # we could otherwise avoid it
	    if dep == self.members[dep.name]: return

	    # merge the flags, and add the newly created dependency
	    # into the class
	    dep = self.members[dep.name].mergeFlags(dep, mergeType = mergeType)
	    del self.members[dep.name]

        grpDep = dependencyCache.setdefault(dep, dep)

	self.members[grpDep.name] = grpDep
	assert(not self.justOne or len(self.members) == 1)

    def score(self, requirements):
	if self.tag != requirements.tag:
	    return False
        
        score = 0
	for requiredDep in requirements.members.itervalues():
	    if not self.members.has_key(requiredDep.name):
                thisScore = requiredDep.emptyDepsScore()
            else:
                thisScore = self.members[requiredDep.name].score(requiredDep)
            if thisScore is False:
                return False

            score += thisScore

        return thisScore

    def emptyDepsScore(self):
        score = 0
	for requiredDep in self.members.itervalues():
            thisScore = requiredDep.emptyDepsScore()
            if thisScore is False:
                return False
            score += thisScore
        return thisScore

    def toStrongFlavor(self):
        newDepClass = self.__class__()
        for dep in self.members.values():
            newDepClass.addDep(dep.toStrongFlavor())
        return newDepClass

    def satisfies(self, requirements):
        return self.score(requirements) is not False

    def union(self, other, mergeType = DEP_MERGE_TYPE_NORMAL):
	if other is None: return
	for otherdep in other.members.itervalues():
	    # calling this for duplicates is a noop
	    self.addDep(otherdep, mergeType = mergeType)

    def __and__(self, other):
        return self.intersection(other)

    def intersection(self, other):
        newDepClass = self.__class__()
        found = False
	for tag, dep in self.members.iteritems():
            if tag in other.members:
                dep = dep.intersection(other.members[tag])
                if dep is None:
                    continue
                newDepClass.addDep(dep)
                found = True
        if found:
            return newDepClass
        return None

    def difference(self, other):
        newDepClass = self.__class__()
        found = False
	for tag, dep in self.members.iteritems():
            if tag in other.members:
                diff = dep.difference(other.members[tag])
                if diff is None:
                    continue
                newDepClass.addDep(diff)
            else:
                newDepClass.addDep(dep)
            found = True
        if found:
            return newDepClass
        else:
            return None

    def __sub__(self, other):
        return self.difference(other)

    def getDeps(self):
        l = self.members.items()
        # sort by name
        l.sort()
        for name, dep in l:
            yield dep

    def thawDependency(frozen):
        frozen = frozen.replace(':', '\0')
        frozen = frozen.replace('\0\0', ':')
        l = frozen.split('\0')
        # that version is a third faster than:
        # frozen = frozen.replace('::', '\0')
        # l = [ x.replace('\0', ':') for x in frozen.split(':') ]
        flags = l[1:]

        for i, flag in enumerate(flags):
            kind = flag[0:2]

            if kind == '~!':
                flags[i] = (flag[2:], FLAG_SENSE_PREFERNOT)
            elif kind[0] == '!':
                flags[i] = (flag[1:], FLAG_SENSE_DISALLOWED)
            elif kind[0] == '~':
                flags[i] = (flag[1:], FLAG_SENSE_PREFERRED)
            else:
                flags[i] = (flag, FLAG_SENSE_REQUIRED)

        d = Dependency(l[0], flags)
        cached = dependencyCache.setdefault(d, d)

        return cached
    thawDependency = staticmethod(thawDependency)

    def __hash__(self):
	val = self.tag
	for dep in self.members.itervalues():
	    val ^= hash(dep)

	return val

    def __eq__(self, other):
        if other is None:
            return False
	return self.tag == other.tag and \
	       self.members == other.members

    def __ne__(self, other):
        return not self == other

    def __str__(self):
	memberList = self.members.items()
	memberList.sort()
	return "\n".join([ "%s: %s" % (self.tagName, dep[1]) 
		    for dep in memberList ])

    def __init__(self):
	self.members = {}

class AbiDependency(DependencyClass):

    tag = DEP_CLASS_ABI
    tagName = "abi"
    justOne = False
    depClass = Dependency
    hasFlags = True
_registerDepClass(AbiDependency)

class InstructionSetDependency(DependencyClass):

    tag = DEP_CLASS_IS
    tagName = "is"
    justOne = False
    depClass = Dependency
    allowParseDep = False
    flags = DEP_CLASS_HAS_FLAGS
_registerDepClass(InstructionSetDependency)

class OldSonameDependencies(DependencyClass):

    tag = DEP_CLASS_OLD_SONAME
    tagName = "oldsoname"
    justOne = False
    depClass = Dependency
    allowParseDep = False
_registerDepClass(OldSonameDependencies)

class SonameDependencies(DependencyClass):

    tag = DEP_CLASS_SONAME
    tagName = "soname"
    justOne = False
    depClass = Dependency
    depFormat = 'IDENT(?:/WORD)*/WORD'
    flags = DEP_CLASS_HAS_FLAGS
_registerDepClass(SonameDependencies)

class UserInfoDependencies(DependencyClass):

    tag = DEP_CLASS_USERINFO
    tagName = "userinfo"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_NO_FLAGS
_registerDepClass(UserInfoDependencies)

class GroupInfoDependencies(DependencyClass):

    tag = DEP_CLASS_GROUPINFO
    tagName = "groupinfo"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_NO_FLAGS
_registerDepClass(GroupInfoDependencies)

class CILDependencies(DependencyClass):

    tag = DEP_CLASS_CIL
    tagName = "CIL"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_HAS_FLAGS
    depFormat = 'IDENT(?:\.IDENT)*' # foo[.bar]*
    flagFormat = '[0-9.]+'          # 0-9[.0-9]*
_registerDepClass(CILDependencies)

class JavaDependencies(DependencyClass):

    tag = DEP_CLASS_JAVA
    tagName = "java"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_HAS_FLAGS
_registerDepClass(JavaDependencies)

class FileDependencies(DependencyClass):

    tag = DEP_CLASS_FILES
    tagName = "file"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_NO_FLAGS
    depFormat = '(?:/WORD)+' # /path[/path]*

_registerDepClass(FileDependencies)

class TroveDependencies(DependencyClass):

    tag = DEP_CLASS_TROVES
    tagName = "trove"
    justOne = False
    depClass = Dependency
    flags = DEP_CLASS_OPT_FLAGS
    depFormat = 'IDENT(?::IDENT)?' # trove[:comp] 

    def thawDependency(frozen):
        d = DependencyClass.thawDependency(frozen)
        cached = dependencyCache.setdefault(d, d)
        return cached

    thawDependency = staticmethod(thawDependency)

_registerDepClass(TroveDependencies)

class UseDependency(DependencyClass):

    tag = DEP_CLASS_USE
    tagName = "use"
    justOne = True
    depClass = Dependency
    allowParseDep = False
_registerDepClass(UseDependency)

class DependencySet(object):

    __slots__ = ( 'members', 'hash' )

    def addDep(self, depClass, dep):
	assert(isinstance(dep, Dependency))
        self.hash = None

	tag = depClass.tag
        c = self.members.setdefault(tag, depClass())
        c.addDep(dep)

    def addDeps(self, depClass, deps):
        self.hash = None
        tag = depClass.tag
        c = self.members.setdefault(tag, depClass())

        for dep in deps:
            c.addDep(dep)

    def iterDeps(self):
        for depClass in self.members.itervalues():
            for dep in depClass.members.itervalues():
                yield depClass.__class__, dep

    def iterDepsByClass(self, depClass):
        if depClass.tag in self.members:
            c = self.members[depClass.tag]
            for dep in c.members.itervalues():
                yield dep

    def hasDepClass(self, depClass):
        return depClass.tag in self.members

    def removeDeps(self, depClass, deps):
        self.hash = None
        c = self.members[depClass.tag]
        for dep in deps:
            del c.members[dep.name]

    def addEmptyDepClass(self, depClass):
        """ adds an empty dependency class, which for flavors has 
            different semantics when merging than not having a dependency 
            class.  See mergeFlavors """
        self.hash = None
	tag = depClass.tag
        assert(tag not in self.members)
        self.members[tag] = depClass()

    def copy(self):
        new = DependencySet()
        new.members = copy.deepcopy(self.members)
        return new

    def toStrongFlavor(self):
        newDep = DependencySet()
        for tag, depClass in self.members.iteritems():
            newDep.members[tag] = depClass.toStrongFlavor()
        return newDep

    def score(self,other):
        score = 0
	for tag in other.members:
            # ignore empty dep classes when scoring
            if not other.members[tag].members:
                continue
	    if tag not in self.members: 
                thisScore = other.members[tag].emptyDepsScore()
            else:
                thisScore = self.members[tag].score(other.members[tag])
            if thisScore is False:
		return False

            score += thisScore

        return score

    def satisfies(self, other):
        return self.score(other) is not False

    def stronglySatisfies(self, other):
        return self.toStrongFlavor().score(
                    other.toStrongFlavor()) is not False

    def getDepClasses(self):
        return self.members

    def union(self, other, mergeType = DEP_MERGE_TYPE_NORMAL):
        if not other:
            return

        self.hash = None

	for tag in other.members:
	    if self.members.has_key(tag):
		self.members[tag].union(other.members[tag],
                                        mergeType = mergeType)
	    else:
		self.members[tag] = copy.deepcopy(other.members[tag])

    def intersection(self, other):
        newDep = DependencySet()
        for tag, depClass in self.members.iteritems():
            if tag in other.members:
                dep = depClass.intersection(other.members[tag])
                if dep is None:
                    continue
                newDep.members[depClass.tag] = dep
        return newDep

    def __and__(self, other):
        return self.intersection(other)

    def difference(self, other):
        newDep = DependencySet()
        for tag, depClass in self.members.iteritems():
            if tag in other.members:
                dep = depClass.difference(other.members[tag])
                if dep is not None:
                    newDep.members[tag] = dep
            else:
                newDep.members[tag] = copy.deepcopy(depClass)
        return newDep

    def __sub__(self, other):
        return self.difference(other)

    def __eq__(self, other):
        if other is None:
            return False
        if other.members.keys() != self.members.keys():
            return False
	for tag in other.members:
	    if not self.members.has_key(tag): 
		return False
	    if not self.members[tag] == other.members[tag]:
		return False

	return True

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        if self.hash is None:
            self.hash = 0
            for member in self.members.itervalues():
                self.hash ^= hash(member)

	return self.hash

    def __nonzero__(self):
	return not(not(self.members))

    def __str__(self):
        if self.isFlavor():
            return formatFlavor(self)
        else:
            memberList = self.members.items()
            memberList.sort()
            return "\n".join([ str(x[1]) for x in memberList])

    def freeze(self):
        rc = []
        for tag, depclass in sorted(self.getDepClasses().items()):
            for dep in depclass.getDeps():
                rc.append('%d#%s' %(tag, dep.freeze()))
        return '|'.join(rc)

    def isFlavor(self):
        for key in self.getDepClasses().iterkeys():
            if key not in (DEP_CLASS_IS, DEP_CLASS_USE):
                return False
        return True

    def __repr__(self):
        if self.isFlavor():
            return "Flavor('%s')" % formatFlavor(self)
        else:
            return "ThawDep('%s')" % self.freeze()

    def __init__(self):
	self.members = {}
        self.hash = None

def ThawDependencySet(frz):
    depSet = DependencySet()
    if frz == 'none' or frz is None:
        return depSet
    l = frz.split('|')
    for line in l:
        if not line:
            continue
        tag, frozen = line.split('#', 1)
        tag = int(tag)
        depClass = dependencyClasses[tag]
        depSet.addDep(depClass, depClass.thawDependency(frozen))
    return depSet

def overrideFlavor(oldFlavor, newFlavor, mergeType=DEP_MERGE_TYPE_OVERRIDE):
    """ 
    Performs overrides of flavors as expected when the new flavor is 
    specified by a user -- the user's flavor overrides use flags, and 
    if the user specifies any instruction sets, only those instruction
    sets will be in the final flavor.  Flags for the specified instruction
    sets are merged with the old flavor.
    """
    flavor = oldFlavor.copy()
    ISD = InstructionSetDependency
    if (flavor.hasDepClass(ISD) and newFlavor.hasDepClass(ISD)):

        arches = set()

        for dep in newFlavor.iterDepsByClass(ISD):
            arches.add(dep.name)

        oldArches = []
        for dep in oldFlavor.iterDepsByClass(ISD):
            if dep.name not in arches:
                oldArches.append(dep)
        flavor.removeDeps(ISD, oldArches)
            
    flavor.union(newFlavor, mergeType=mergeType)
    return flavor

def mergeFlavor(flavor, mergeBase):
    """ 
    Merges the given flavor with the mergeBase - if flavor 
    doesn't contain use flags, then include the mergeBase's 
    use flags.  If flavor doesn't contain an instruction set, then 
    include the mergeBase's instruction set(s)
    """
    if flavor is None:
        return mergeBase
    if not mergeBase:
        return flavor
    needsIns = not flavor.hasDepClass(InstructionSetDependency)
    needsUse = not flavor.hasDepClass(UseDependency)
    if not (needsIns or needsUse):
        return flavor

    mergedFlavor = flavor.copy()
    if needsIns:
        insSets = list(mergeBase.iterDepsByClass(InstructionSetDependency))
        if insSets:
            mergedFlavor.addDeps(InstructionSetDependency, insSets)

    if needsUse:
        useSet = list(mergeBase.iterDepsByClass(UseDependency))
        if useSet:
            mergedFlavor.addDeps(UseDependency, useSet)
    return mergedFlavor

def formatFlavor(flavor):
    """
    Formats a flavor and returns a string which parseFlavor can 
    handle.
    """
    def _singleClass(deps):
        l = []
        for dep in deps:
            flags = dep.getFlags()[0]

            if flags:
                flags.sort()
                l.append("%s(%s)" % (dep.getName()[0],
                           ",".join([ "%s%s" % (senseMap[x[1]], x[0]) 
                                                for x in flags])))
            else:
                l.append(dep.getName()[0])

        l.sort()
        return " ".join(l)

    classes = flavor.getDepClasses()
    insSet = list(flavor.iterDepsByClass(InstructionSetDependency))
    useFlags = list(flavor.iterDepsByClass(UseDependency))

    if insSet:
        insSet = _singleClass(insSet)

    if useFlags:
        # strip the use() bit
        useFlags = _singleClass(useFlags)[4:-1]

    if insSet and useFlags:
        return "%s is: %s" % (useFlags, insSet)
    elif insSet:
        return "is: %s" % insSet
    elif useFlags:
        return useFlags

    return ""

def parseFlavor(s, mergeBase = None):
    # return a DependencySet for the string passed. format is
    # [arch[(flag,[flag]*)]] [use:flag[,flag]*]
    #
    # if mergeBase is set, the parsed flavor is merged into it. The
    # rules for the merge are different than those for union() though;
    # the parsed flavor is assumed to set the is:, use:, or both. If
    # either class is unset, it's taken from mergeBase.

    def _fixup(flag):
        flag = flag.strip()
        if senseReverseMap.has_key(flag[0:2]):
            sense = senseReverseMap[flag[0:2]]
            flag = flag[2:]
        elif senseReverseMap.has_key(flag[0]):
            sense = senseReverseMap[flag[0]]
            flag = flag[1:]
        else:
            sense = FLAG_SENSE_REQUIRED

        return (flag, sense)

    s = s.strip()

    match = flavorRegexp.match(s)
    if not match:
        return None

    groups = match.groups()

    set = DependencySet()

    if groups[3]:
        # groups[3] is base instruction set, groups[4] is the flags, and
        # groups[5] is the next instruction set

        # set up the loop for the next pass
        insGroups = groups[3:]
        while insGroups[0]:
            # group 0 is the base, group[1] is the flags, and group[2] is
            # the next instruction set clause
            baseInsSet = insGroups[0]

            if insGroups[1]:
                insSetFlags = insGroups[1].split(",")
                for i, flag in enumerate(insSetFlags):
                    insSetFlags[i] = _fixup(flag)
            else:
                insSetFlags = []

            set.addDep(InstructionSetDependency, Dependency(baseInsSet, 
                                                            insSetFlags))

            if not insGroups[2]:
                break

            match = archGroupRegexp.match(insGroups[2])
            # this had to match, or flavorRegexp wouldn't have
            assert(match)
            insGroups = match.groups()

    elif groups[2]:
        # mark that the user specified "is:" without any instruction set
        # by adding a placeholder instruction set dep class here. 
        set.addEmptyDepClass(InstructionSetDependency)

    if groups[1]:
        useFlags = groups[1].split(",")
        for i, flag in enumerate(useFlags):
            useFlags[i] = _fixup(flag)

        set.addDep(UseDependency, Dependency("use", useFlags))
    elif groups[0]:
        # mark that the user specified "use:" without any instruction set
        # by adding a placeholder instruction set dep class here. 
        set.addEmptyDepClass(UseDependency)

    return mergeFlavor(set, mergeBase)

    return set

def parseDep(s):
    """ 
    Parses dependency strings (not flavors) of the format 
    (<depClass>: dep[(flags)])* and returns a dependency set
    containing those dependencies.
    Raises ParseError if the parsing fails.
    """
    
    depSet = DependencySet()
    while s:
        match = depRegexp.match(s)

        if not match:
            raise ParseError, ('depString starting at %s'
                               ' is not a valid dep string' % s)

        tagName = match.groups()[0]
        depClause = match.groups()[1]
        wholeMatch = match.group()
        s = s[len(wholeMatch):]

        if tagName not in dependencyClassesByName:
            raise ParseError, ('no such dependency class %s' % tagName)

        depClass = dependencyClassesByName[tagName]

        # depRegexp matches a generic depClass: dep(flags) set
        # - pass the dep to the given depClass for parsing
        dep = depClass.parseDep(depClause)
        assert(dep is not None)
        depSet.addDep(depClass, dep)
    return depSet

def flavorDifferences(flavors):
    """ Takes a set of flavors, returns a dict of flavors such that 
        the value of a flavor's dict entry is a flavor that includes 
        only the information that differentiates that flavor from others
        in the set
    """
    diffs = {}
    base = flavors[0].copy()
    # the intersection of all the flavors will provide the largest common
    # flavor that is shared between all the flavors given
    for flavor in flavors[1:]:
        base = base & flavor
    # remove the common flavor bits
    for flavor in flavors:
        diffs[flavor] = flavor - base
    return diffs



dependencyCache = util.ObjectCache()

ident = '(?:[0-9A-Za-z_-]+)'
flag = '(?:~?!?IDENT)'
useFlag = '(?:!|~!)?FLAG(?:\.IDENT)?'
archFlags = '\(( *FLAG(?: *, *FLAG)*)\)'
archClause = '(?:(IDENT)(?:ARCHFLAGS)?)?'
archGroup = '(?:ARCHCLAUSE(?:  *(ARCHCLAUSE))*)'
useClause = '(USEFLAG *(?:, *USEFLAG)*)?'


depFlags = ' *(?:\([^)]*\))? *' # anything inside parens
depName = r'(?:[^ (]+)' # anything except for a space or an opening paren
depClause = depName + depFlags
depRegexpStr = r'(IDENT): *(DEPCLAUSE) *'

flavorRegexpStr = '^(use:)? *(?:USECLAUSE)? *(?:(is:) *ARCHGROUP)?$'

flavorRegexpStr = flavorRegexpStr.replace('ARCHGROUP', archGroup)
flavorRegexpStr = flavorRegexpStr.replace('ARCHCLAUSE', archClause)
flavorRegexpStr = flavorRegexpStr.replace('ARCHFLAGS', archFlags)
flavorRegexpStr = flavorRegexpStr.replace('USECLAUSE', useClause)
flavorRegexpStr = flavorRegexpStr.replace('USEFLAG', useFlag)
flavorRegexpStr = flavorRegexpStr.replace('FLAG', flag)
flavorRegexpStr = flavorRegexpStr.replace('IDENT', ident)
flavorRegexp = re.compile(flavorRegexpStr)

archGroupStr = archGroup.replace('ARCHCLAUSE', archClause)
archGroupStr = archGroupStr.replace('ARCHFLAGS', archFlags)
archGroupStr = archGroupStr.replace('USECLAUSE', useClause)
archGroupStr = archGroupStr.replace('USEFLAG', useFlag)
archGroupStr = archGroupStr.replace('FLAG', flag)
archGroupStr = archGroupStr.replace('IDENT', ident)
archGroupRegexp = re.compile(archGroupStr)

depRegexpStr = depRegexpStr.replace('DEPCLAUSE', depClause)
depRegexpStr = depRegexpStr.replace('IDENT', ident)
depRegexp = re.compile(depRegexpStr)


del ident, flag, useFlag, archClause, useClause, flavorRegexpStr
del depFlags, depName, depClause, depRegexpStr
del archGroupStr

# None means disallowed match
flavorScores = {
      (FLAG_SENSE_UNSPECIFIED, FLAG_SENSE_REQUIRED ) : None,
      (FLAG_SENSE_UNSPECIFIED, FLAG_SENSE_DISALLOWED):    0,
      (FLAG_SENSE_UNSPECIFIED, FLAG_SENSE_PREFERRED) :   -1,
      (FLAG_SENSE_UNSPECIFIED, FLAG_SENSE_PREFERNOT) :    1,

      (FLAG_SENSE_REQUIRED,    FLAG_SENSE_REQUIRED ) :    2,
      (FLAG_SENSE_REQUIRED,    FLAG_SENSE_DISALLOWED): None,
      (FLAG_SENSE_REQUIRED,    FLAG_SENSE_PREFERRED) :    1,
      (FLAG_SENSE_REQUIRED,    FLAG_SENSE_PREFERNOT) : None,

      (FLAG_SENSE_DISALLOWED,  FLAG_SENSE_REQUIRED ) : None,
      (FLAG_SENSE_DISALLOWED,  FLAG_SENSE_DISALLOWED):    2,
      (FLAG_SENSE_DISALLOWED,  FLAG_SENSE_PREFERRED) : None,
      (FLAG_SENSE_DISALLOWED,  FLAG_SENSE_PREFERNOT) :    1,

      (FLAG_SENSE_PREFERRED,   FLAG_SENSE_REQUIRED ) :    1,
      (FLAG_SENSE_PREFERRED,   FLAG_SENSE_DISALLOWED): None,
      (FLAG_SENSE_PREFERRED,   FLAG_SENSE_PREFERRED) :    2,
      (FLAG_SENSE_PREFERRED,   FLAG_SENSE_PREFERNOT) :   -1,

      (FLAG_SENSE_PREFERNOT,   FLAG_SENSE_REQUIRED ) :   -2,
      (FLAG_SENSE_PREFERNOT,   FLAG_SENSE_DISALLOWED):    1,
      (FLAG_SENSE_PREFERNOT,   FLAG_SENSE_PREFERRED) :   -1,
      (FLAG_SENSE_PREFERNOT,   FLAG_SENSE_PREFERNOT) :    1 
}


class ParseError(Exception):

    """
    Indicates that an error occurred turning a string into an object
    in the dependency module.
    """

    def __str__(self):
	return self.str

    def __init__(self, str):
	self.str = str