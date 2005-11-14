from conary.build import macros
from conary.build import use
from conary.build.recipe import Recipe, RECIPE_TYPE_REDIRECT

class RedirectRecipe(Recipe):
    Flags = use.LocalFlags
    _recipeType = RECIPE_TYPE_REDIRECT
    ignore = 1

    def addRedirect(self, name, versionStr = None, flavorStr = None,
                    fromTrove = None):
        if flavorStr is not None:
            flavor = deps.parseFlavor(flavorStr)
            if flavor is None:
                raise ValueError, 'invalid flavor %s' % flavorStr
        else:
            flavor = None

        if fromTrove is None:
            fromTrove = self.name
        elif fromTrove.find(":") != -1:
            raise ValueError, 'components cannot be individually redirected'

        self.addTroveList.append((name, versionStr, flavor, fromTrove))

    def findTroves(self):
        self.size = 0

        validSize = True
        troveList = []

        packageSet = {}

        for (name, versionStr, flavor, fromName) in self.addTroveList:
            try:
                desFlavor = self.cfg.buildFlavor.copy()
                if flavor is not None:
                    desFlavor.union(flavor, deps.DEP_MERGE_TYPE_OVERRIDE)
                pkgList = self.repos.findTrove(self.branch.label(), 
                                               (name, versionStr, desFlavor))
            except errors.TroveNotFound, e:
                raise RecipeFileError, "Couldn't find redirect trove: " + str(e)

            assert(len(pkgList) == 1)
            packageSet[pkgList[0]] = fromName
            troveList.append(pkgList[0])

        troves = self.repos.getTroves(troveList, withFiles = False)
        for topLevelTrove in troves:
            topName = topLevelTrove.getName()
            topVersion = topLevelTrove.getVersion()
            topFlavor = topLevelTrove.getFlavor()
            fromName = packageSet[(topName, topVersion, topFlavor)]

            d = self.redirections.setdefault(fromName, set())

            # this redirects from oldTrove -> newTrove
            d.add((topName, topVersion, topFlavor))

            for (name, version, flavor) in topLevelTrove.iterTroveList():
                # redirect from oldTrove -> referencedPackage
                d.add((name, version, flavor))

                if name.find(":") != -1:
                    compName = fromName + ":" + name.split(":")[1]
                    # redirect from oldTrove -> oldTrove:component. we
                    # leave version/flavor alone; they get filled in later
                    d.add((compName, None, None))

                    # redirect from oldTrove:component -> newTrove:component
                    d2 = self.redirections.setdefault(compName, set())
                    d2.add((name, version, flavor))

        allComps = self.repos.getCollectionMembers(self.name, self.branch)
        for compName in allComps:
            if compName in self.redirections: continue
            self.redirections[compName] = set()
            self.redirections[self.name].add((compName, None, None))

    def getRedirections(self):
	return self.redirections

    def __init__(self, repos, cfg, branch, flavor, extraMacros={}):
	self.repos = repos
	self.cfg = cfg
        self.redirections = {}
	self.branch = branch
	self.flavor = flavor
        self.addTroveList = []
        self.macros = macros.Macros()
        self.macros.update(extraMacros)

