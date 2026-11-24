package fixture;
import java.util.Arrays;
import java.util.List;

public class Main {
  public static List<String> tokenize(String text) { return Arrays.asList(text.trim().split("\\s+")); }
  public static int score(List<String> parts) { return parts.size(); }
}
