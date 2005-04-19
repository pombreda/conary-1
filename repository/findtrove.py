
from deps import deps
import repository
import versions


######################################
# Query Types
# findTroves divides queries up into a set of sub queries, depending on 
# how the trove is to be found
# Below are the five different types of queries that can be created 
# from findTroves

QUERY_BY_VERSION           = 0
QUERY_BY_BRANCH            = 1
QUERY_BY_LABEL_PATH        = 2
QUERY_REVISION_BY_LABEL    = 3
QUERY_REVISION_BY_BRANCH   = 4
QUERY_SENTINEL             = 5

queryTypes = range(QUERY_SENTINEL)

#################################
# VersionStr Types 
# Different version string types, plus affinity troves if available, 
# result in different queries

VERSION_STR_NONE                 = 0
VERSION_STR_FULL_VERSION         = 1 # branch + trailing revision
VERSION_STR_BRANCH               = 2 # branch
VERSION_STR_LABEL                = 3 # host@namespace:tag
VERSION_STR_BRANCHNAME           = 4 # @namespace:tag
VERSION_STR_TAG                  = 5 # :tag
VERSION_STR_REVISION             = 6 # troveversion-sourcecount[-buildcount]
VERSION_STR_TROVE_VER            = 7 # troveversion (no source or build count)

class Query:
    def __init__(self, defaultFlavorPath, labelPath, acrossRepositories,
                                                     acrossFlavors):
        self.map = {}
        self.defaultFlavorPath = defaultFlavorPath
        if self.defaultFlavorPath is None:
            self.query = [{}]
        else:
            self.query = [{} for x in defaultFlavorPath ]
        self.labelPath = labelPath
        self.acrossRepositories = acrossRepositories
        self.acrossFlavors = acrossFlavors

    def reset(self):
        if self.defaultFlavorPath is None:
            self.query = [{}]
        else:
            self.query = [{} for x in self.defaultFlavorPath]
        self.map = {}

    def hasName(self, name):
        return name in self.map

    def hasTroves(self):
        return bool(self.map)

    def findAll(self, repos, missing, finalMap):
        raise NotImplementedError

    def filterTroveMatches(self, name, versionFlavorDict):
        """ filter the versions and flavors returns from a repository 
            query based on criterea specific to the query.
            Returns a { version : [flavor...] } dict.
        """
        # identity filter 
        return versionFlavorDict

    def overrideFlavors(self, flavor):
        """ override the flavors in the defaultFlavorPath with flavor,
            replacing instruction set entirely if given.
        """
        if not self.defaultFlavorPath:
            return [flavor]
        flavors = []
        for defaultFlavor in self.defaultFlavorPath:
            flavors.append(deps.overrideFlavor(defaultFlavor, flavor, 
                                        mergeType = deps.DEP_MERGE_TYPE_PREFS)) 
        return flavors

    def addQuery(self, troveTup, *params):
        raise NotImplementedError

    def addMissing(self, missing, name):
        troveTup = self.map[name]
        missing[troveTup] = self.missingMsg(name)
            
    def missingMsg(self, name):
        versionStr = self.map[name][1]
        if not versionStr:
            return ("%s was not on found on path %s" \
                    % (name, ', '.join(x.asString() for x in self.labelPath)))
        elif self.labelPath:
            return ("version %s of %s was not on found on path %s" \
                    % (versionStr, name, 
                       ', '.join(x.asString() for x in labelPath)))
        else:
            return "version %s of %s was not on found" % (versionStr, name)

class QueryByVersion(Query):

    def __init__(self, defaultFlavor, labelPath, acrossRepositories, 
                                                 acrossFlavors):
        Query.__init__(self, defaultFlavor, labelPath, acrossRepositories, 
                                                       acrossFlavors)
        self.queryNoFlavor = {}

    def reset(self):
        Query.reset(self)
        self.queryNoFlavor = {}

    def addQuery(self, troveTup, version, flavorList):
        name = troveTup[0]
        self.map[name] = troveTup
        if flavorList is None:
            self.queryNoFlavor[name] = { version : None }
        else:
            for i, flavor in enumerate(flavorList):
                self.query[i][name] = {version : [flavor] } 

    def addQueryWithAffinity(self, troveTup, version, affinityTroves):
        flavors = [x[2] for x in affinityTroves]
        f = flavors[0]
        for otherFlavor in flavors:
            if otherFlavor != f:
                # bail if there are two affinity flavors
                f = None
                break
        if f is None:
            flavorList = self.defaultFlavorPath
        else:
            flavorList = self.overrideFlavors(f)  

        self.addQuery(troveTup, version, flavorList)

    def findAll(self, repos, missing, finalMap):
        self._findAllNoFlavor(repos, missing, finalMap)
        self._findAllFlavor(repos, missing, finalMap)

    def _findAllFlavor(self, repos, missing, finalMap):
        namesToFind = set(self.query[0])
        foundNames = set()
        for query in self.query:
            # delete any found names - don't search for them again
            for name in foundNames:
                query.pop(name, None)
            res = repos.getTroveVersionFlavors(query, bestFlavor=True)
            for name in res:
                foundNames.add(name)
                namesToFind.remove(name)
                pkgList = []
                for version, flavorList in res[name].iteritems():
                    pkgList.extend((name, version, f) for f in flavorList)
                finalMap[self.map[name]] = pkgList

        for name in namesToFind:
            self.addMissing(missing, name)

    def _findAllNoFlavor(self, repos, missing, finalMap):
        res = repos.getTroveVersionFlavors(self.queryNoFlavor, bestFlavor=False)
        for name in self.queryNoFlavor:
            if name not in res or not res[name]:
                self.addMissing(missing, name)
                continue
            pkgList = []
            for version, flavorList in res[name].iteritems():
                pkgList.extend((name, version, f) for f in flavorList)
            finalMap[self.map[name]] = pkgList

    def missingMsg(self, name):
        versionStr = self.map[name][1]
        return "version %s of %s was not on found" % (versionStr, name)

class QueryByLabelPath(Query):

    def __init__(self, *args, **kw):
        Query.__init__(self, *args, **kw)
        self.query = {}
    
    def reset(self):
        self.query = {}
        self.map = {}

    def addQuery(self, troveTup, labelPath, flavorList):
        name = troveTup[0]
        self.map[name] = troveTup

        if self.acrossRepositories:
            if flavorList is None:
                self.query[name] = [ dict.fromkeys(labelPath, None)]
            elif self.acrossFlavors:
                # create one big query: {name : [{label  : [flavor1, flavor2],
                #                                 label2 : [flavor1, flavor2]}
 
                d = {}
                for label in labelPath:
                    d[label] = flavorList[:]
                self.query[name] = [d]
            else:
                # create a set of queries like {name : [{label  : [flavor1],
                #                                        label2 : [flavor1]},
                #                                       {label : [flavor2],
                #                                        label2 : [flavor2]}
                # -- if flavor1 is found on label1 or label2, stop searching
                # on that label for this name.  Otherwise, continue searching 
                # using flavor1
                self.query[name] = []
                for flavor in flavorList:
                    d = {}
                    self.query[name].append(d)
                    for label in labelPath:
                        d[label] = [flavor]
        else:
            self.query[name] = []
            if flavorList is None:
                for label in labelPath:
                    self.query[name].append({label : None})
            elif self.acrossFlavors:
                # create a set of queries:
                #  query[name] = [ {label  : [flavor1, flavor2],
                #                   label2 : [flavor1, flavor2]},
                for label in labelPath:
                    self.query[name].append({label : flavorList[:]})
            else:
                # create a set of queries:
                # query[name] = [ {label: [flavor1]}, {label: [flavor2]}, 
                #                 {label2 : [flavor1}, {label2: [flavor2]} --
                # search label 1 for all flavors on the flavorPath before
                # searching label 2
                for label in labelPath:
                    for flavor in flavorList:
                        self.query[name].append({label : [flavor]})

    def addQueryWithAffinity(self, troveTup, labelPath, affinityTroves):
        name = troveTup[0]
        self.map[name] = troveTup

        for label in labelPath:
            flavors = []
            for (afName, afVersion, afFlavor) in affinityTroves:
                if afVersion.branch().label() == label:
                    flavors.append(afFlavor)
            if not flavors:
                f = None
            else:
                f = flavors[0]
                for otherFlavor in flavors:
                    if otherFlavor != f:
                        f = None
                        break
            if f is None:
                flavorList = self.defaultFlavorPath
            else:
                flavorList = self.overrideFlavors(f)  
            self.addQuery(troveTup, labelPath, flavorList) 

    def callQueryFunction(self, repos, query):
        return repos.getTroveLeavesByLabel(query, bestFlavor=True)
        
    def findAll(self, repos, missing, finalMap):

        index = 0
        namesToFind = set(self.query)
        foundNames = set()
        if self.acrossRepositories:
            foundNameLabels = set()
        # self.query[name] is an ordered list of queries to use 
        # for that name.  If name is found using one query, then
        # stop searching for that name (unless acrossRepositories 
        # is used, in which case a name/label pair must be found)
        while self.query:
            labelQuery = {}

            # compile a query from all of the query[name] components  
            for name in self.query.keys():
                try:
                    req = self.query[name][index]
                except IndexError:
                    if name not in foundNames:
                        self.addMissing(missing, name)
                        namesToFind.remove(name)
                    del(self.query[name])
                    continue

                if self.acrossRepositories:
                    # if we're searching across repositories, 
                    # we are trying to find one match per label
                    # if we've already found a match for a label, 
                    # remove it
                    for label in req:
                        if (name, label) in foundNameLabels:
                            req.pop(label)
                elif name in foundNames:
                    continue
                labelQuery[name] = req

            if not labelQuery:
                break

            # call the query
            res = self.callQueryFunction(repos, labelQuery)

            for name in res:
                if not res[name]:
                    continue
                # filter the query -- this is overridden in 
                # QueryByLabelRevision
                matches = self.filterTroveMatches(name, res[name])
                if not matches: 
                    continue

                # found name, don't search for it any more
                foundNames.add(name)
                namesToFind.remove(name)

                pkgList = []
                for version, flavorList in matches.iteritems():
                    pkgList.extend((name, version, f) for f in flavorList)

                    if self.acrossRepositories:
                        foundNameLabels.add((name, 
                                             version.branch().label()))
                finalMap.setdefault(self.map[name], []).extend(pkgList)
            index +=1

    def missingMsg(self, name):
        labelPath = [ x.keys()[0] for x in self.query[name] ]
        return "%s was not on found on path %s" \
                % (name, ', '.join(x.asString() for x in labelPath))

class QueryByBranch(Query):

    def __init__(self, defaultFlavor, labelPath, acrossRepositories, 
                                                 acrossFlavors):
        Query.__init__(self, defaultFlavor, labelPath, acrossRepositories,
                                                       acrossFlavors)
        self.queryNoFlavor = {}
        self.affinityFlavors = {}

    def reset(self):
        Query.reset(self)
        self.queryNoFlavor = {}
        self.affinityFlavors = {}

    def addQuery(self, troveTup, branch, flavorList):
        name = troveTup[0]

        if flavorList is None:
            self.queryNoFlavor[name] = { branch : None }
        else:
            for i, flavor in enumerate(flavorList):
                if name not in self.query[i]:
                    self.query[i][name] = { branch: []}
                elif branch not in self.query[i][name]:
                    self.query[i][name][branch] = []
                self.query[i][name][branch].append(flavor)
        self.map[name] = troveTup 

    def addQueryWithAffinity(self, troveTup, branch, affinityTroves):
        if branch:
            # use the affinity flavor if it's the same for all troves, 
            # otherwise revert to the default flavor
            flavors = [x[2] for x in affinityTroves]
            f = flavors[0]
            for otherFlavor in flavors:
                if otherFlavor != f:
                    f = None
                    break
            if f is None:
                flavorList = self.defaultFlavorPath
            else:
                flavorList = self.overrideFlavors(f)

            self.addQuery(troveTup, branch, flavorList)
        else:
            for dummy, afVersion, afFlavor in affinityTroves:
                flavorList = self.overrideFlavors(afFlavor)
                self.addQuery(troveTup, afVersion.branch(), flavorList)


    def findAll(self, repos, missing, finalMap):
        self._findAllNoFlavor(repos, missing, finalMap)
        self._findAllFlavor(repos, missing, finalMap)

    def callQueryFunction(self, repos, query):
        return repos.getTroveLeavesByBranch(query, bestFlavor=True)

    def _findAllFlavor(self, repos, missing, finalMap):
        namesToFind = set(self.query[0])
        foundBranches = set()
        foundNames = set()

        for query in self.query:
            for name, branch in foundBranches:
                query[name].pop(branch, None)
                if not query[name]:
                    del query[name]
            if not query:
                break
            res = self.callQueryFunction(repos, query)
            if not res:
                continue
            for name in res:
                matches = self.filterTroveMatches(name, res[name])

                if not matches:
                    continue

                foundNames.add(name)
                try:
                    namesToFind.remove(name)
                except KeyError:
                    pass
                pkgList = []
                for version, flavorList in matches.iteritems():
                    pkgList.extend((name, version, f) for f in flavorList)
                    foundBranches.add((name, version.branch()))
                finalMap.setdefault(self.map[name], []).extend(pkgList)
        for name in namesToFind:
            self.addMissing(missing, name)

    def _findAllNoFlavor(self, repos, missing, finalMap):
        res = repos.getTroveLeavesByBranch(self.queryNoFlavor, bestFlavor=False)
        for name in self.queryNoFlavor:
            if name not in res or not res[name]:
                self.addMissing(missing, name)
                continue
            pkgList = []
            for version, flavorList in res[name].iteritems():
                pkgList.extend((name, version, f) for f in flavorList)
            finalMap[self.map[name]] = pkgList

    def missingMsg(self, name):
        flavor = self.map[name][2]
        if flavor is None:
            branches = self.queryNoFlavor[name].keys()
        else:
            branches = self.query[0][name].keys()
        return "%s was not on found on branches %s" \
                % (name, ', '.join(x.asString() for x in branches))

class QueryRevisionByBranch(QueryByBranch):

    def addQuery(self, troveTup, branch, flavorList):
        # QueryRevisionByBranch is only reached when a revision is specified
        # for findTrove and an affinity trove was found.  flavorList should
        # not be empty.
        assert(flavorList is not None)
        QueryByBranch.addQuery(self, troveTup, branch, flavorList)

    def callQueryFunction(self, repos, query):
        return repos.getTroveVersionsByBranch(query, bestFlavor=True)

    def filterTroveMatches(self, name, versionFlavorDict):
        versionStr = self.map[name][1]
        try:
            verRel = versions.Revision(versionStr)
        except versions.ParseError, e:
            verRel = None

        for version in reversed(sorted(versionFlavorDict.iterkeys())):
            if verRel:
                if version.trailingRevision() != verRel:
                    continue
            else:
                if version.trailingRevision().version != versionStr:
                    continue
            return { version: versionFlavorDict[version] }
        return {}

    def missingMsg(self, name):
        branch = self.query[0][name].keys()[0]
        versionStr = self.map[name][1]
        return "revision %s of %s was found on branch %s" \
                                    % (versionStr, name, branch.asString())

class QueryRevisionByLabel(QueryByLabelPath):

    queryFunctionName = 'getTroveVersionsByLabel'

    def callQueryFunction(self, repos, query):
        return repos.getTroveVersionsByLabel(query, bestFlavor=True)

    def filterTroveMatches(self, name, versionFlavorDict):
        """ Take the results found in QueryByLabelPath.findAll for name
            and filter them based on if they match the given revision
            for name.  Return a versionFlavorDict
        """

        matching = {}
        matchingLabels = set()

        versionStr = self.map[name][1]
        try:
            verRel = versions.Revision(versionStr)
        except versions.ParseError, e:
            verRel = None
        for version in reversed(sorted(versionFlavorDict.iterkeys())):
            if verRel:
                if version.trailingRevision() != verRel:
                    continue
            else:
                if version.trailingRevision().version \
                                                != versionStr:
                    continue
            if not self.acrossRepositories:
                # there should be only one label in this versionFlavorDict --
                # so, optimize to return first result found
                return {version: versionFlavorDict[version]}

            label = version.branch().label()
            if label in matchingLabels:
                continue
            matchingLabels.add(label)
            matching[version] = versionFlavorDict[version]
        return matching

    def missingMsg(self, name):
        labelPath = [ x.keys()[0] for x in self.query[name] ]
        versionStr = self.map[name][1]
        return "revision %s of %s was not found on label(s) %s" \
                % (versionStr, name, 
                   ', '.join(x.asString() for x in labelPath))

##############################################
# 
# query map from enumeration to classes that define how to grab 
# the related troves

queryTypeMap = { QUERY_BY_BRANCH            : QueryByBranch,
                 QUERY_BY_VERSION           : QueryByVersion,
                 QUERY_BY_LABEL_PATH        : QueryByLabelPath, 
                 QUERY_REVISION_BY_LABEL    : QueryRevisionByLabel, 
                 QUERY_REVISION_BY_BRANCH   : QueryRevisionByBranch,
               }

def getQueryClass(tag):
    return queryTypeMap[tag]


##########################################################


class TroveFinder:
    """ find troves by sorting them into query types by the version string
        and then calling those query types.   
    """

    def findTroves(self, repos, troveSpecs, allowMissing=False):
        finalMap = {}

        while troveSpecs:
            self.remaining = []

            for troveSpec in troveSpecs:
                self.addQuery(troveSpec)

            missing = {}

            for query in self.query.values():
                query.findAll(repos, missing, finalMap)
                query.reset()

            if missing and not allowMissing:
                if len(missing) > 1:
                    missingMsgs = [ missing[x] for x in troveSpecs if x in missing]
                    raise repository.TroveNotFound, '%d troves not found:\n%s\n' \
                            % (len(missing), '\n'.join(x for x in missingMsgs))
                else:
                    raise repository.TroveNotFound, missing.values()[0]

            troveSpecs = self.remaining

        return finalMap

    def addQuery(self, troveTup):
        (name, versionStr, flavor) = troveTup
        if not self.labelPath and versionStr[0] != "/":
            raise repository.TroveNotFound, \
                "fully qualified version or label " + \
                "expected instead of %s" % versionStr

        affinityTroves = []
        if self.affinityDatabase:
            try:
                affinityTroves = self.affinityDatabase.findTrove(None, 
                                                                 troveTup[0])
            except repository.TroveNotFound:
                pass
        
        type = self._getVersionType(troveTup)
        sortFn = self.getVersionStrSortFn(type)
        sortFn(self, troveTup, affinityTroves) 

    ########################
    # The following functions translate from the version string in the
    # trove spec to the type of query that will actually find the trove(s)
    # corresponding to this trove spec.  We call this sorting the trovespec
    # into the correct query.

    def _getVersionType(self, troveTup):
        """
        Return a string that describes this troveTup's versionStr
        The string returned corresponds to a function name for sorting on 
        that versionStr type.
        """
        name = troveTup[0]
        versionStr = troveTup[1]
        if not versionStr:
            return VERSION_STR_NONE
        firstChar = versionStr[0]
        if firstChar == '/':
            try:
                version = versions.VersionFromString(versionStr)
            except versions.ParseError, e:
                raise repository.TroveNotFound, str(e)
            if isinstance(version, versions.Branch):
                return VERSION_STR_BRANCH
            else:
                return VERSION_STR_FULL_VERSION
        elif versionStr.find('/') != -1:
            # if we've got a version string, and it doesn't start with a
            # /, no / is allowed
            raise repository.TroveNotFound, \
                    "incomplete version string %s not allowed" % versionStr
        elif firstChar == '@':
            return VERSION_STR_BRANCHNAME
        elif firstChar == ':':
            return VERSION_STR_TAG
        elif versionStr.count('@'):
            return VERSION_STR_LABEL
        else:
            for char in ' ,':
                if char in versionStr:
                    raise RuntimeError, \
                        ('%s reqests illegal version/revision %s' 
                                                % (name, versionStr))
            if '-' in versionStr:
                try:
                    verRel = versions.Revision(versionStr)
                    return VERSION_STR_REVISION
                except ParseError, msg:
                    raise repository.TroveNotFound, str(msg)
            return VERSION_STR_TROVE_VER

    def sortNoVersion(self, troveTup, affinityTroves):
        name, versionStr, flavor = troveTup
        if flavor is None and affinityTroves:
            if self.query[QUERY_BY_BRANCH].hasName(name):
                self.remaining.append(troveTup)
                return
            self.query[QUERY_BY_BRANCH].addQueryWithAffinity(troveTup, None, 
                                                             affinityTroves)
        elif self.query[QUERY_BY_LABEL_PATH].hasName(name):
            self.remaining.append(troveTup)
            return
        else:
            flavorList = self.mergeFlavors(flavor)
            self.query[QUERY_BY_LABEL_PATH].addQuery(troveTup,
                                                     self.labelPath, 
                                                     flavorList)

    def sortBranch(self, troveTup, affinityTroves):
        name, versionStr, flavor = troveTup
        if self.query[QUERY_BY_BRANCH].hasName(name):
            self.remaining.append(troveTup)
            return
        branch = versions.VersionFromString(versionStr)
        if flavor is None and affinityTroves:
            self.query[QUERY_BY_BRANCH].addQueryWithAffinity(troveTup, branch, 
                                                             affinityTroves)
        else:
            flavorList = self.mergeFlavors(flavor)
            self.query[QUERY_BY_BRANCH].addQuery(troveTup, branch, flavorList)

    def sortFullVersion(self, troveTup, affinityTroves):
        name, versionStr, flavor = troveTup
        if self.query[QUERY_BY_VERSION].hasName(name):
            self.remaining.append(tup)
            return
        version = versions.VersionFromString(versionStr)
        if flavor is None and affinityTroves:
            self.query[QUERY_BY_VERSION].addQueryWithAffinity(troveTup, 
                                                              version,
                                                              affinityTroves)
        else:
            flavorList = self.mergeFlavors(flavor)
            self.query[QUERY_BY_VERSION].addQuery(troveTup, version, flavorList)

    def sortLabel(self, troveTup, affinityTroves):
        try:
            label = versions.Label(troveTup[1])
            newLabelPath = [ label ]
        except versions.ParseError:
            raise repository.TroveNotFound, \
                                "invalid version %s" % versionStr
        return self._sortLabel(newLabelPath, troveTup, affinityTroves)

    def sortBranchName(self, troveTup, affinityTroves):
        # just a branch name was specified
        repositories = [ x.getHost() for x in self.labelPath ]
        versionStr = troveTup[1]
        newLabelPath = []
        for serverName in repositories:
            newLabelPath.append(versions.Label("%s%s" %
                                               (serverName, versionStr)))
        return self._sortLabel(newLabelPath, troveTup, affinityTroves)
        
    def sortTag(self, troveTup, affinityTroves):
        repositories = [(x.getHost(), x.getNamespace()) \
                         for x in self.labelPath ]
        newLabelPath = []
        versionStr = troveTup[1]
        for serverName, namespace in repositories:
            newLabelPath.append(versions.Label("%s@%s%s" %
                               (serverName, namespace, versionStr)))
        return self._sortLabel(newLabelPath, troveTup, affinityTroves)

    def _sortLabel(self, labelPath, troveTup, affinityTroves):
        if self.query[QUERY_BY_LABEL_PATH].hasName(troveTup[0]): 
            self.remaining.append(troveTup)
            return
        flavor = troveTup[2]
        if flavor is None and affinityTroves:
            self.query[QUERY_BY_LABEL_PATH].addQueryWithAffinity(troveTup, 
                                                    labelPath, affinityTroves)
        else:
            flavorList = self.mergeFlavors(flavor)
            self.query[QUERY_BY_LABEL_PATH].addQuery(troveTup, labelPath, 
                                                     flavorList)

    def sortTroveVersion(self, troveTup, affinityTroves):
        name = troveTup[0]
        flavor = troveTup[2]
        if flavor is None and affinityTroves:
            if self.query[QUERY_REVISION_BY_BRANCH].hasName(name):
                self.remaining.append(tup)
                return
            self.query[QUERY_REVISION_BY_BRANCH].addQueryWithAffinity(troveTup,
                                                          None, affinityTroves)
        elif self.query[QUERY_REVISION_BY_LABEL].hasName(name):
            self.remaining.append(troveTup)
            return
        else:
            flavorList = self.mergeFlavors(flavor)
            self.query[QUERY_REVISION_BY_LABEL].addQuery(troveTup, 
                                                         self.labelPath, 
                                                         flavorList)

    def getVersionStrSortFn(self, versionStrType):
        return self.versionStrToSortFn[versionStrType]

    def mergeFlavors(self, flavor):
        """ Merges the given flavor with the flavorPath - if flavor 
            doesn't contain use flags, then include the defaultFlavor's 
            use flags.  If flavor doesn't contain an instruction set, then 
            include the flavorpath's instruction set(s)
        """
        if flavor is None:
            return self.defaultFlavorPath
        if not self.defaultFlavorPath:
            return [flavor]
        return [ deps.mergeFlavor(flavor, x) for x in self.defaultFlavorPath ]

    def __init__(self, labelPath, defaultFlavorPath, acrossRepositories, 
                 acrossFlavors, affinityDatabase):
        self.affinityDatabase = affinityDatabase
        self.acrossRepositories = acrossRepositories
        self.acrossFlavors = acrossFlavors
        if labelPath and not type(labelPath) == list:
            labelPath = [ labelPath ]
        self.labelPath = labelPath

        if defaultFlavorPath is not None and not isinstance(defaultFlavorPath,
                                                            list):
            defaultFlavorPath = [defaultFlavorPath]
        self.defaultFlavorPath = defaultFlavorPath


        self.remaining = []
        self.query = {}
        for queryType in queryTypes:
            self.query[queryType] = getQueryClass(queryType)(defaultFlavorPath, 
                                                             labelPath, 
                                                             acrossRepositories,
                                                             acrossFlavors)
    # class variable for TroveFinder
    #
    # set up map from a version string type to the source fn to use
    versionStrToSortFn = \
             { VERSION_STR_NONE         : sortNoVersion,
               VERSION_STR_FULL_VERSION : sortFullVersion,
               VERSION_STR_BRANCH       : sortBranch,
               VERSION_STR_LABEL        : sortLabel,
               VERSION_STR_BRANCHNAME   : sortBranchName,
               VERSION_STR_TAG          : sortTag,
               VERSION_STR_REVISION     : sortTroveVersion,
               VERSION_STR_TROVE_VER    : sortTroveVersion }

