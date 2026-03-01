FROM eclipse-temurin:17-jdk-jammy
WORKDIR /app
# Pre-compile a dummy file to initialize the javac compiler
RUN echo "public class Main { public static void main(String[] args) {} }" > Main.java
RUN javac Main.java
ENTRYPOINT ["sh", "-c", "javac Main.java && java Main"]