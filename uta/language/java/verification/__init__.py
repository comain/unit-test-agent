from __future__ import annotations

class JavaVerificationRunner:
    language = "java"

    def verify(self, repo_path, target, **kwargs):
        from uta.language.java.verification.runner import verify_java_target

        return verify_java_target(repo_path, target, **kwargs)
