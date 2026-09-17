package com.ofdrwconverter.cli;

import org.ofdrw.converter.export.TextExporter;
import org.ofdrw.reader.OFDReader;

import java.io.IOException;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * OFD 纯文本导出命令行入口。
 *
 * <p>内部使用 {@code org.ofdrw.converter.export.TextExporter} 将 OFD 板式文档导出为纯文本，
 * 并通过标准输出 / 结果文件向调用方（Python）返回机器可读的 JSON 结果。</p>
 *
 * <pre>
 * 用法:
 *   java -jar ofdrw-text-cli.jar --input file.ofd --output out.txt [--pages 1,3,5-7] [--result r.json]
 * </pre>
 *
 * @author ofdrw-converter plugin
 */
public final class Main {

    /** CLI 版本号，与 pom.xml 保持同步。 */
    private static final String VERSION = "1.0.0";

    private Main() {
    }

    public static void main(String[] args) {
        Options opt;
        try {
            opt = Options.parse(args);
        } catch (IllegalArgumentException e) {
            System.err.println("参数错误: " + e.getMessage());
            System.err.println();
            System.err.println(Options.USAGE);
            System.exit(2);
            return;
        }

        if (opt.help) {
            System.out.println(Options.USAGE);
            System.exit(0);
            return;
        }

        long started = System.currentTimeMillis();
        try {
            run(opt, started);
        } catch (Throwable t) {
            StringBuilder sb = new StringBuilder(256);
            sb.append("{\"ok\":false,\"error\":{");
            sb.append("\"type\":");
            appendJsonString(sb, t.getClass().getName());
            sb.append(",\"message\":");
            appendJsonString(sb, String.valueOf(t.getMessage()));
            sb.append("}}");
            String json = sb.toString();
            // 结果文件优先，便于调用方稳定读取；同时输出到 stdout 便于人工排查。
            if (opt.resultFile != null) {
                try {
                    Files.createDirectories(opt.resultFile.toAbsolutePath().getParent());
                    Files.write(opt.resultFile, json.getBytes(StandardCharsets.UTF_8));
                } catch (IOException ignored) {
                    // 忽略：下面仍会输出到 stdout
                }
            }
            System.out.println(json);
            t.printStackTrace(System.err);
            System.exit(1);
        }
    }

    private static void run(Options opt, long started) throws Exception {
        Path input = opt.inputFile;
        if (!Files.exists(input)) {
            throw new IOException("OFD 文件不存在: " + input);
        }
        if (Files.isDirectory(input)) {
            throw new IOException("输入路径是目录而不是 OFD 文件: " + input);
        }

        Path output = opt.outputFile;
        if (output == null) {
            String name = input.getFileName().toString();
            int dot = name.lastIndexOf('.');
            String base = dot > 0 ? name.substring(0, dot) : name;
            output = input.toAbsolutePath().getParent().resolve(base + ".txt");
        }
        output = output.toAbsolutePath();

        // 先读取文档页数：既用于页码合法性校验，也能在文档损坏时给出明确错误。
        int pageCount;
        try (OFDReader reader = new OFDReader(input)) {
            pageCount = reader.getNumberOfPages();
        }

        List<Integer> ignored = new ArrayList<>();
        int[] indexes;
        if (opt.pageIndexes == null) {
            indexes = new int[0]; // 空数组 = 导出全部页
        } else {
            List<Integer> valid = new ArrayList<>(opt.pageIndexes.size());
            for (Integer idx : opt.pageIndexes) {
                if (idx < 0 || idx >= pageCount) {
                    ignored.add(idx);
                } else {
                    valid.add(idx);
                }
            }
            indexes = new int[valid.size()];
            for (int i = 0; i < valid.size(); i++) {
                indexes[i] = valid.get(i);
            }
        }

        // TextExporter 内部使用 PrintStream(默认字符集) 写文件；
        // 为兼容关闭了 UTF-8 默认编码的 JVM，导出后统一转码为 UTF-8。
        Path tmpDir = Files.createTempDirectory("ofdrw-text-");
        Path rawTxt = tmpDir.resolve("raw.txt");
        try {
            try (TextExporter exporter = new TextExporter(input, rawTxt)) {
                exporter.export(indexes);
            }

            Charset def = Charset.defaultCharset();
            String text;
            if (StandardCharsets.UTF_8.equals(def)) {
                text = new String(Files.readAllBytes(rawTxt), StandardCharsets.UTF_8);
            } else {
                text = new String(Files.readAllBytes(rawTxt), def);
            }

            // TextExporter 通过 PrintStream.println 写出，行分隔符跟随平台（Windows 上是 CRLF）。
            // 统一归一化为 LF，保证跨平台输出一致，也保证与调用方按文本读取时的字符计数一致。
            text = text.replace("\r\n", "\n").replace('\r', '\n');

            Files.createDirectories(output.getParent());
            Files.write(output, text.getBytes(StandardCharsets.UTF_8));

            long elapsed = System.currentTimeMillis() - started;
            int lineCount = 0;
            for (int i = 0; i < text.length(); i++) {
                if (text.charAt(i) == '\n') {
                    lineCount++;
                }
            }
            if (text.length() > 0 && !text.endsWith("\n")) {
                lineCount++;
            }

            StringBuilder sb = new StringBuilder(512);
            sb.append("{\"ok\":true");
            sb.append(",\"version\":");
            appendJsonString(sb, VERSION);
            sb.append(",\"input\":");
            appendJsonString(sb, input.toAbsolutePath().toString());
            sb.append(",\"output\":");
            appendJsonString(sb, output.toString());
            sb.append(",\"pageCount\":").append(pageCount);
            sb.append(",\"exportedPageCount\":").append(indexes.length == 0 ? pageCount : indexes.length);
            sb.append(",\"charCount\":").append(text.length());
            sb.append(",\"lineCount\":").append(lineCount);
            sb.append(",\"empty\":").append(text.trim().isEmpty());
            sb.append(",\"ignoredPages\":[");
            for (int i = 0; i < ignored.size(); i++) {
                if (i > 0) {
                    sb.append(',');
                }
                sb.append(ignored.get(i) + 1); // 转回 1 起页码便于人工阅读
            }
            sb.append("]");
            sb.append(",\"charset\":\"UTF-8\"");
            sb.append(",\"elapsedMs\":").append(elapsed);
            sb.append('}');

            String json = sb.toString();
            if (opt.resultFile != null) {
                Files.createDirectories(opt.resultFile.toAbsolutePath().getParent());
                Files.write(opt.resultFile, json.getBytes(StandardCharsets.UTF_8));
            }
            System.out.println(json);

            if (text.trim().isEmpty()) {
                System.err.println("[warn] 导出结果为空：该 OFD 可能整页为图片或路径图元，无法提取文本。");
            }
        } finally {
            deleteQuietly(rawTxt);
            deleteQuietly(tmpDir);
        }
    }

    private static void deleteQuietly(Path p) {
        try {
            if (p != null && Files.exists(p)) {
                Files.delete(p);
            }
        } catch (IOException ignored) {
            // 临时文件清理失败不影响主流程
        }
    }

    /** 以 ASCII 安全方式写入 JSON 字符串（非 ASCII 一律转义为 \\uXXXX）。 */
    private static void appendJsonString(StringBuilder sb, String s) {
        if (s == null) {
            sb.append("null");
            return;
        }
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                case '\b':
                    sb.append("\\b");
                    break;
                case '\f':
                    sb.append("\\f");
                    break;
                default:
                    if (c < 0x20 || c > 0x7e) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }

    /** 命令行参数。 */
    private static final class Options {

        static final String USAGE = String.join("\n",
                "ofdrw-text-cli " + VERSION + " - 将 OFD 文档导出为纯文本 (基于 ofdrw-converter TextExporter)",
                "",
                "用法:",
                "  java -jar ofdrw-text-cli.jar --input <file.ofd> [--output <out.txt>]",
                "                                  [--pages <1,3,5-7>] [--result <result.json>]",
                "",
                "参数:",
                "  --input,  -i   必填，待转换的 OFD 文件路径",
                "  --output, -o   可选，导出的纯文本文件路径；默认与输入同目录同名 .txt",
                "  --pages,  -p   可选，要导出的页码（1 起，支持 1,3,5-7 形式）；默认导出全部页",
                "  --result, -r   可选，将 JSON 结果写入该文件（便于程序稳定读取）",
                "  --help,   -h   显示本帮助",
                "",
                "退出码:",
                "  0 成功   1 转换失败   2 参数错误");

        final Path inputFile;
        final Path outputFile;
        final Path resultFile;
        final List<Integer> pageIndexes;
        final boolean help;

        private Options(Path inputFile, Path outputFile, Path resultFile,
                        List<Integer> pageIndexes, boolean help) {
            this.inputFile = inputFile;
            this.outputFile = outputFile;
            this.resultFile = resultFile;
            this.pageIndexes = pageIndexes;
            this.help = help;
        }

        static Options parse(String[] args) {
            Path input = null;
            Path output = null;
            Path result = null;
            List<Integer> pages = null;
            boolean help = false;

            for (int i = 0; i < args.length; i++) {
                String a = args[i];
                switch (a) {
                    case "--input":
                    case "-i":
                        input = Paths.get(requireValue(args, ++i, a));
                        break;
                    case "--output":
                    case "-o":
                        output = Paths.get(requireValue(args, ++i, a));
                        break;
                    case "--result":
                    case "-r":
                        result = Paths.get(requireValue(args, ++i, a));
                        break;
                    case "--pages":
                    case "-p":
                        pages = parsePages(requireValue(args, ++i, a));
                        break;
                    case "--help":
                    case "-h":
                        help = true;
                        break;
                    default:
                        throw new IllegalArgumentException("未知参数: " + a);
                }
            }

            if (!help && input == null) {
                throw new IllegalArgumentException("缺少必填参数 --input");
            }
            return new Options(input, output, result, pages, help);
        }

        private static String requireValue(String[] args, int i, String flag) {
            if (i >= args.length) {
                throw new IllegalArgumentException("参数 " + flag + " 缺少取值");
            }
            return args[i];
        }

        /** 解析形如 {@code 1,3,5-7} 的页码串，转换成 0 起索引。 */
        private static List<Integer> parsePages(String spec) {
            Set<Integer> out = new LinkedHashSet<>();
            for (String partRaw : spec.split(",")) {
                String part = partRaw.trim();
                if (part.isEmpty()) {
                    continue;
                }
                int dash = part.indexOf('-');
                if (dash > 0 && dash < part.length() - 1) {
                    int from = parsePositiveInt(part.substring(0, dash).trim());
                    int to = parsePositiveInt(part.substring(dash + 1).trim());
                    int step = from <= to ? 1 : -1;
                    for (int p = from; p != to + step; p += step) {
                        out.add(p - 1);
                    }
                } else {
                    out.add(parsePositiveInt(part) - 1);
                }
            }
            return new ArrayList<>(out);
        }

        private static int parsePositiveInt(String s) {
            int v;
            try {
                v = Integer.parseInt(s);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException("非法页码: " + s);
            }
            if (v < 1) {
                throw new IllegalArgumentException("页码必须从 1 开始: " + s);
            }
            return v;
        }
    }
}
