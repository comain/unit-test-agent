package uta.maven;

import java.lang.reflect.Field;
import java.io.File;
import java.io.IOException;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.util.Arrays;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import java.util.WeakHashMap;
import java.util.regex.Pattern;
import javax.xml.parsers.DocumentBuilderFactory;
import javax.xml.transform.TransformerFactory;
import javax.xml.transform.dom.DOMSource;
import javax.xml.transform.stream.StreamResult;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;
import org.apache.maven.artifact.versioning.DefaultArtifactVersion;
import org.apache.maven.AbstractMavenLifecycleParticipant;
import org.apache.maven.MavenExecutionException;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.execution.MojoExecutionEvent;
import org.apache.maven.execution.MojoExecutionListener;
import org.apache.maven.model.Plugin;
import org.apache.maven.model.PluginExecution;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Restores late PIT property binding; filter-diff remains the sole scope authority. */
public class PitRuntimeCompatibility extends AbstractMavenLifecycleParticipant implements MojoExecutionListener {
    private static final Map<MavenSession, Map<MavenProject, Scope>> SCOPES =
            Collections.synchronizedMap(new WeakHashMap<MavenSession, Map<MavenProject, Scope>>());
    private static final Map<MavenSession, Map<MavenProject, Completion>> COMPLETED =
            Collections.synchronizedMap(new WeakHashMap<MavenSession, Map<MavenProject, Completion>>());

    private static boolean enabled(MavenSession session) {
        return "diff-enforcement".equals(session.getUserProperties().getProperty("uta.pit.compat.intent"))
                && "true".equals(session.getUserProperties().getProperty("test.enforcement.enabled"));
    }

    @Override
    public void afterProjectsRead(MavenSession session) throws MavenExecutionException {
        if (!enabled(session)) {
            return;
        }
        if (!"3.9.10".equals(MavenSession.class.getPackage().getImplementationVersion())
                || !"1.8".equals(System.getProperty("java.specification.version"))) {
            throw new MavenExecutionException("[uta-pit-compat] Validated runtime requires Maven 3.9.10 and Java 8", (Throwable) null);
        }
        for (String key : Arrays.asList("skipPitest", "targetClasses", "excludedMethods")) {
            if (session.getUserProperties().containsKey(key) || session.getSystemProperties().containsKey(key)) {
                throw new MavenExecutionException("[uta-pit-compat] Reserved scope property: " + key, (Throwable) null);
            }
        }
        SCOPES.put(session, Collections.synchronizedMap(new LinkedHashMap<MavenProject, Scope>()));
        COMPLETED.put(session, Collections.synchronizedMap(new LinkedHashMap<MavenProject, Completion>()));
        int index = 0;
        for (MavenProject project : session.getProjects()) {
            SCOPES.get(session).put(project, null);
            for (Plugin plugin : project.getBuildPlugins()) {
                normalize(plugin, session, index++);
            }
            if (project.getPluginManagement() != null) {
                for (Plugin plugin : project.getPluginManagement().getPlugins()) {
                    normalize(plugin, session, index++);
                }
            }
        }
    }

    private static void normalize(Plugin plugin, MavenSession session, int index) throws MavenExecutionException {
        if (!"org.pitest:pitest-maven".equals(plugin.getKey())) {
            return;
        }
        if (!"1.15.0".equals(plugin.getVersion())) {
            throw new MavenExecutionException("[uta-pit-compat] Unsupported PIT version: " + plugin.getVersion(), (Throwable) null);
        }
        String evidence = session.getUserProperties().getProperty("uta.pit.compat.evidence");
        File directory = evidence == null ? null : new File(new File(evidence).getParentFile(), "pit-" + index);
        plugin.setConfiguration(normalizeConfiguration(plugin.getConfiguration(), session, directory));
        int executionIndex = 0;
        for (PluginExecution execution : plugin.getExecutions()) {
            File report = directory == null ? null : new File(directory, "execution-" + executionIndex++);
            execution.setConfiguration(normalizeConfiguration(execution.getConfiguration(), session, report));
        }
    }

    private static Xpp3Dom normalizeConfiguration(Object source, MavenSession session, File directory) {
        Xpp3Dom config = source instanceof Xpp3Dom ? new Xpp3Dom((Xpp3Dom) source) : new Xpp3Dom("configuration");
        // DMS's profile interpolated skip=false before filter-diff. Do not copy
        // that literal back: absent configuration uses PIT's native late binding.
        for (String key : Arrays.asList("skip", "targetClasses", "excludedMethods", "testStrengthThreshold")) {
            remove(config, key);
        }
        // The released test-enforcer owns the changed-line threshold. PIT only
        // produces XML; its class-wide threshold must not reject legacy lines.
        Xpp3Dom threshold = new Xpp3Dom("testStrengthThreshold");
        threshold.setValue("0");
        config.addChild(threshold);
        // filter-diff owns production scope. A POM exclusion (RedisClient* in
        // the incident) must not silently subtract an authoritative target.
        remove(config, "excludedClasses");
        Xpp3Dom excludedClasses = new Xpp3Dom("excludedClasses");
        excludedClasses.setAttribute("combine.self", "override");
        config.addChild(excludedClasses);
        // skipFailingTests has no user property in pitest-maven 1.15.0, so a -D on
        // the command line is silently ignored and the run keeps the descriptor
        // default false. Binding it here is the only way to reach it without a
        // plugin release; afterMojoExecutionSuccess then asserts PIT really took it,
        // because "silently ignored" is exactly how this was missed before.
        for (String key : Arrays.asList("targetTests", "excludedTestClasses", "skipFailingTests")) {
            if (session.getUserProperties().containsKey(key)) {
                remove(config, key);
                Xpp3Dom value = new Xpp3Dom(key);
                value.setValue("${" + key + "}");
                config.addChild(value);
            }
        }
        if (directory != null) {
            set(config, "reportsDirectory", directory.getAbsolutePath());
            set(config, "timestampedReports", "false");
            Xpp3Dom formats = config.getChild("outputFormats");
            if (formats == null) {
                formats = new Xpp3Dom("outputFormats");
                config.addChild(formats);
                Xpp3Dom html = new Xpp3Dom("param");
                html.setValue("HTML");
                formats.addChild(html);
            }
            boolean xml = false;
            for (Xpp3Dom child : formats.getChildren()) {
                xml |= "XML".equals(child.getValue());
            }
            if (!xml) {
                Xpp3Dom format = new Xpp3Dom("param");
                format.setValue("XML");
                formats.addChild(format);
            }
        }
        return config;
    }

    private static void set(Xpp3Dom config, String key, String value) {
        remove(config, key);
        Xpp3Dom child = new Xpp3Dom(key);
        child.setValue(value);
        config.addChild(child);
    }

    private static void remove(Xpp3Dom config, String key) {
        for (int index = config.getChildCount() - 1; index >= 0; index--) {
            if (key.equals(config.getChild(index).getName())) {
                config.removeChild(index);
            }
        }
    }

    private static boolean filter(MojoExecutionEvent event) {
        return "com.example.build.maven-plugins".equals(event.getExecution().getGroupId())
                && "test-enforcer".equals(event.getExecution().getArtifactId())
                && "filter-diff".equals(event.getExecution().getGoal());
    }

    private static boolean pit(MojoExecutionEvent event) {
        return "org.pitest".equals(event.getExecution().getGroupId())
                && "pitest-maven".equals(event.getExecution().getArtifactId())
                && "mutationCoverage".equals(event.getExecution().getGoal());
    }

    @Override
    public void beforeMojoExecution(MojoExecutionEvent event) throws MojoExecutionException {
        if (!enabled(event.getSession())) {
            return;
        }
        Map<MavenProject, Scope> scopes = SCOPES.get(event.getSession());
        if (scopes == null) {
            throw new MojoExecutionException("[uta-pit-compat] Lifecycle initialization missing");
        }
        if (filter(event)) {
            String version = event.getExecution().getVersion();
            if (version.contains("SNAPSHOT") || new DefaultArtifactVersion(version).compareTo(new DefaultArtifactVersion("1.0.16")) < 0) {
                throw new MojoExecutionException("[uta-pit-compat] test-enforcer >= 1.0.16 release required; found " + version);
            }
            // The scope is unauthoritative until this execution republishes it,
            // but the PIT run already recorded under the previous scope is not
            // wrong yet: test-enforcer runs filter-diff again after
            // mutationCoverage, and dropping the record here reported a
            // completed module as missing. afterMojoExecutionSuccess keeps it
            // only if the republished scope is identical.
            scopes.put(event.getProject(), null);
        }
        if (!pit(event)) {
            return;
        }
        Scope scope = scopes.get(event.getProject());
        if (scope == null) {
            throw new MojoExecutionException("[uta-pit-compat] PIT requires a successful filter-diff first");
        }
        try {
            // check-mutation reads Maven's conventional report path. Remove any
            // previous run before PIT starts; only a validated report from this
            // invocation is published back to that path after PIT succeeds.
            Files.deleteIfExists(standardMutationReport(event.getProject()).toPath());
            Object mojo = event.getMojo();
            boolean skip = (Boolean) field(mojo, "skip");
            @SuppressWarnings("unchecked")
            Collection<String> targets = (Collection<String>) mojo.getClass().getMethod("getTargetClasses").invoke(mojo);
            Set<String> resolved = new LinkedHashSet<String>(targets);
            if (skip != scope.skip || !resolved.equals(scope.targets)) {
                throw new MojoExecutionException("[uta-pit-compat] Injected PIT scope differs from filter-diff for "
                        + event.getProject().getArtifactId());
            }
            if (!list(mojo, "getExcludedMethods").equals(scope.excludedMethods)
                    || ((Number) field(mojo, "testStrengthThreshold")).intValue() != 0) {
                throw new MojoExecutionException("[uta-pit-compat] PIT exclusions or changed-line threshold isolation failed");
            }
            if (!list(mojo, "getExcludedClasses").isEmpty()) {
                throw new MojoExecutionException("[uta-pit-compat] PIT excludedClasses would override filter-diff targets");
            }
            for (String key : Arrays.asList("targetTests", "excludedTestClasses")) {
                String expected = event.getSession().getUserProperties().getProperty(key);
                String getter = "get" + Character.toUpperCase(key.charAt(0)) + key.substring(1);
                if (expected != null && !list(mojo, getter).equals(values(expected))) {
                    throw new MojoExecutionException("[uta-pit-compat] PIT selection mismatch: " + key);
                }
            }
            String skipFailing = event.getSession().getUserProperties().getProperty("skipFailingTests");
            if (skipFailing != null
                    && ((Boolean) field(mojo, "skipFailingTests")).booleanValue()
                            != Boolean.parseBoolean(skipFailing)) {
                throw new MojoExecutionException("[uta-pit-compat] PIT skipFailingTests not applied");
            }
            System.out.println("[uta-pit-compat] verified module=" + event.getProject().getArtifactId()
                    + " skip=" + skip + " targets=" + resolved.size()
                    + " skipFailingTests=" + field(mojo, "skipFailingTests"));
        } catch (IOException e) {
            throw new MojoExecutionException("[uta-pit-compat] Cannot clear stale mutation evidence", e);
        } catch (ReflectiveOperationException | ClassCastException e) {
            throw new MojoExecutionException("[uta-pit-compat] Unsupported PIT parameter binding", e);
        }
    }

    @SuppressWarnings("unchecked")
    private static Set<String> list(Object mojo, String method) throws ReflectiveOperationException {
        Collection<String> value = (Collection<String>) mojo.getClass().getMethod(method).invoke(mojo);
        return value == null ? Collections.<String>emptySet() : new LinkedHashSet<String>(value);
    }

    private static Set<String> values(String text) {
        Set<String> result = new LinkedHashSet<String>();
        if (text != null) {
            for (String value : text.split(",")) {
                if (!value.trim().isEmpty()) {
                    result.add(value.trim());
                }
            }
        }
        return result;
    }

    private static Object field(Object value, String name) throws ReflectiveOperationException {
        for (Class<?> type = value.getClass(); type != null; type = type.getSuperclass()) {
            try {
                Field member = type.getDeclaredField(name);
                member.setAccessible(true);
                return member.get(value);
            } catch (NoSuchFieldException ignored) {
                // PIT stores these fields on its shared mojo superclass.
            }
        }
        throw new NoSuchFieldException(name);
    }

    @Override
    public void afterMojoExecutionSuccess(MojoExecutionEvent event) throws MojoExecutionException {
        if (!enabled(event.getSession())) {
            return;
        }
        if (pit(event)) {
            Scope scope = SCOPES.get(event.getSession()).get(event.getProject());
            if (scope == null) {
                throw new MojoExecutionException("[uta-pit-compat] Lost PIT scope");
            }
            String evidence = event.getSession().getUserProperties().getProperty("uta.pit.compat.evidence");
            if (!scope.skip && evidence != null) {
                try {
                    File directory = (File) event.getMojo().getClass().getMethod("getReportsDirectory").invoke(event.getMojo());
                    if (!directory.getCanonicalFile().toPath().startsWith(new File(evidence).getParentFile().getCanonicalFile().toPath())) {
                        throw new MojoExecutionException("[uta-pit-compat] PIT report escaped invocation directory");
                    }
                    File xml = new File(directory, "mutations.xml");
                    if (xml.isFile() && xml.length() > 64L * 1024 * 1024) {
                        throw new MojoExecutionException("[uta-pit-compat] Mutation evidence exceeds size bound for "
                                + event.getProject().getArtifactId());
                    }
                    // A gate with no mutation behind it still cannot pass, but
                    // "evidence went missing" is not what happened: PIT ran this
                    // module under a real target and produced none. Say which,
                    // or the reader goes looking for a lost file.
                    if (!xml.isFile()) {
                        throw new MojoExecutionException("[uta-pit-compat] PIT produced no mutation report for "
                                + event.getProject().getArtifactId() + " (targets=" + scope.targets
                                + "); the changed lines yielded no mutations, so the mutation gate is vacuous");
                    }
                    NodeList mutations = parser().newDocumentBuilder().parse(xml).getElementsByTagName("mutation");
                    if (mutations.getLength() == 0) {
                        throw new MojoExecutionException("[uta-pit-compat] PIT generated zero mutations for "
                                + event.getProject().getArtifactId() + " (targets=" + scope.targets
                                + "); the mutation gate is vacuous");
                    }
                    for (int i = 0; i < mutations.getLength(); i++) {
                        Element mutation = (Element) mutations.item(i);
                        NodeList classes = mutation.getElementsByTagName("mutatedClass");
                        boolean matches = false;
                        if (classes.getLength() == 1) {
                            String name = classes.item(0).getTextContent();
                            for (String target : scope.targets) {
                                String regex = Pattern.quote(target).replace("*", "\\E.*\\Q").replace("?", "\\E.\\Q");
                                matches |= name.matches(regex);
                            }
                        }
                        if (!matches) {
                            throw new MojoExecutionException("[uta-pit-compat] Mutation report contains a class outside filter-diff scope");
                        }
                    }
                    publishMutationReport(xml, standardMutationReport(event.getProject()));
                } catch (MojoExecutionException e) {
                    throw e;
                } catch (Exception e) {
                    throw new MojoExecutionException("[uta-pit-compat] Cannot validate fresh mutation evidence", e);
                }
            }
            COMPLETED.get(event.getSession()).put(event.getProject(),
                    new Completion(scope.skip ? "skipped" : "completed", scope));
        }
        if (!filter(event)) {
            return;
        }
        Map<MavenProject, Scope> scopes = SCOPES.get(event.getSession());
        // The root filter sets properties on every reactor project, including
        // dependencies without their own inherited filter execution.
        for (MavenProject project : event.getSession().getProjects()) {
            String skip = project.getProperties().getProperty("skipPitest");
            String targets = project.getProperties().getProperty("targetClasses");
            if (("true".equals(skip) || "false".equals(skip)) && targets != null && !targets.isEmpty()) {
                boolean sentinel = targets.endsWith(".__no_changes__") && !targets.contains(",");
                if ("true".equals(skip) != sentinel) {
                    throw new MojoExecutionException("[uta-pit-compat] Incoherent filter-diff scope for " + project.getArtifactId());
                }
                Scope republished = new Scope("true".equals(skip), targets,
                        project.getProperties().getProperty("excludedMethods"),
                        project.getProperties().getProperty("test.enforcement.pitest.testStrengthThreshold"));
                scopes.put(project, republished);
                Completion done = COMPLETED.get(event.getSession()).get(project);
                if (done != null && !republished.equals(done.scope)) {
                    COMPLETED.get(event.getSession()).remove(project);
                }
            }
        }
        // A filter-diff that never republished this project's scope leaves it
        // unauthoritative, so whatever PIT recorded under the old one goes too.
        if (scopes.get(event.getProject()) == null) {
            COMPLETED.get(event.getSession()).remove(event.getProject());
        }
    }

    private static File standardMutationReport(MavenProject project) {
        return new File(project.getBasedir(), "target/pit-reports/mutations.xml");
    }

    private static void publishMutationReport(File source, File destination) throws Exception {
        if (source.getCanonicalFile().equals(destination.getCanonicalFile())) {
            return;
        }
        Files.createDirectories(destination.getParentFile().toPath());
        File temporary = Files.createTempFile(destination.getParentFile().toPath(),
                destination.getName() + ".", ".uta.tmp").toFile();
        Files.copy(source.toPath(), temporary.toPath(), StandardCopyOption.REPLACE_EXISTING);
        try {
            Files.move(temporary.toPath(), destination.toPath(),
                    StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } catch (AtomicMoveNotSupportedException ignored) {
            Files.move(temporary.toPath(), destination.toPath(), StandardCopyOption.REPLACE_EXISTING);
        } finally {
            Files.deleteIfExists(temporary.toPath());
        }
    }

    private static DocumentBuilderFactory parser() throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        factory.setFeature("http://xml.org/sax/features/external-general-entities", false);
        factory.setFeature("http://xml.org/sax/features/external-parameter-entities", false);
        factory.setXIncludeAware(false);
        factory.setExpandEntityReferences(false);
        return factory;
    }

    @Override
    public void afterSessionEnd(MavenSession session) throws MavenExecutionException {
        Map<MavenProject, Scope> scopes = SCOPES.remove(session);
        Map<MavenProject, Completion> completed = COMPLETED.remove(session);
        String filename = session.getUserProperties().getProperty("uta.pit.compat.evidence");
        if (scopes == null || filename == null) {
            return;
        }
        try {
            Document document = parser().newDocumentBuilder().newDocument();
            Element root = document.createElement("pitCompatibility");
            root.setAttribute("invocation", session.getUserProperties().getProperty("uta.pit.compat.invocation", ""));
            document.appendChild(root);
            // A later unrelated Maven goal may fail after every gate finished.
            // Only actual PIT completion authorizes that existing UTA policy.
            boolean complete = !scopes.isEmpty();
            for (Map.Entry<MavenProject, Scope> entry : scopes.entrySet()) {
                MavenProject project = entry.getKey();
                if (entry.getValue() == null) {
                    complete = false;
                }
                if ("pom".equals(project.getPackaging())) {
                    continue;
                }
                Completion done = completed.get(project);
                String state = done == null ? null : done.state;
                // No-target dependencies need not declare PIT; obligated modules
                // must finish a real invocation, not borrow another module's green.
                if (state == null) {
                    state = entry.getValue() != null && entry.getValue().skip ? "skipped" : "missing";
                }
                complete &= !"missing".equals(state);
                Element module = document.createElement("module");
                module.setAttribute("id", project.getId());
                module.setAttribute("state", state);
                root.appendChild(module);
            }
            root.setAttribute("complete", String.valueOf(complete));
            File output = new File(filename);
            Files.createDirectories(output.getParentFile().toPath());
            TransformerFactory.newInstance().newTransformer().transform(new DOMSource(document), new StreamResult(output));
        } catch (Exception e) {
            throw new MavenExecutionException("[uta-pit-compat] Cannot write completion evidence", e);
        }
    }

    @Override
    public void afterExecutionFailure(MojoExecutionEvent event) {
        Map<MavenProject, Scope> scopes = SCOPES.get(event.getSession());
        if (scopes != null && filter(event)) {
            scopes.put(event.getProject(), null);
            Map<MavenProject, Completion> completed = COMPLETED.get(event.getSession());
            if (completed != null) {
                completed.remove(event.getProject());
            }
        }
    }

    private static final class Scope {
        private final boolean skip;
        private final Set<String> targets;
        private final Set<String> excludedMethods;
        private final int threshold;

        private Scope(boolean skip, String targets, String excludedMethods, String threshold) {
            this.skip = skip;
            this.targets = new LinkedHashSet<String>(Arrays.asList(targets.split(",")));
            this.excludedMethods = values(excludedMethods);
            this.threshold = Integer.parseInt(threshold);
        }

        @Override
        public boolean equals(Object other) {
            if (!(other instanceof Scope)) {
                return false;
            }
            Scope scope = (Scope) other;
            return skip == scope.skip && threshold == scope.threshold
                    && targets.equals(scope.targets) && excludedMethods.equals(scope.excludedMethods);
        }

        @Override
        public int hashCode() {
            return targets.hashCode() * 31 + excludedMethods.hashCode() + threshold + (skip ? 1 : 0);
        }
    }

    /** One module's PIT outcome, bound to the filter-diff scope it ran under. */
    private static final class Completion {
        private final String state;
        private final Scope scope;

        private Completion(String state, Scope scope) {
            this.state = state;
            this.scope = scope;
        }
    }
}
