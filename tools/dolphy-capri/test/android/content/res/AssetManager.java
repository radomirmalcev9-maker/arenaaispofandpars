package android.content.res;
import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
public class AssetManager {
    public InputStream open(String name) { return new ByteArrayInputStream("[\"bluetooth\",\"other\"]".getBytes(StandardCharsets.UTF_8)); }
}
