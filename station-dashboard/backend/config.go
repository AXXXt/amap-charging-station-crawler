package main

import (
	"bufio"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type Config struct {
	Addr          string
	DSN           string
	AllowedOrigin string
}

func loadConfig() (Config, error) {
	loadEnvFiles()

	dsn, err := databaseDSN()
	if err != nil {
		return Config{}, err
	}
	return Config{
		Addr:          envOrDefault("DASHBOARD_ADDR", "127.0.0.1:8088"),
		DSN:           dsn,
		AllowedOrigin: envOrDefault("DASHBOARD_ALLOWED_ORIGIN", "*"),
	}, nil
}

func loadEnvFiles() {
	candidates := []string{".env", "../.env", "../../.env", "../../../.env"}
	for _, candidate := range candidates {
		file, err := os.Open(filepath.Clean(candidate))
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(file)
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
				continue
			}
			parts := strings.SplitN(line, "=", 2)
			key := strings.TrimSpace(parts[0])
			value := strings.Trim(strings.TrimSpace(parts[1]), `"'`)
			if key != "" && os.Getenv(key) == "" {
				_ = os.Setenv(key, value)
			}
		}
		_ = file.Close()
		return
	}
}

func databaseDSN() (string, error) {
	if raw := strings.TrimSpace(os.Getenv("EVCS_DATABASE_URL")); raw != "" {
		return dsnFromURL(raw)
	}
	host := envOrDefault("DB_HOST", "127.0.0.1")
	port := envOrDefault("DB_PORT", "3306")
	user := os.Getenv("DB_USER")
	password := os.Getenv("DB_PASSWORD")
	name := os.Getenv("DB_NAME")
	if user == "" || name == "" {
		return "", fmt.Errorf("EVCS_DATABASE_URL or DB_USER/DB_NAME is required")
	}
	return fmt.Sprintf("%s:%s@tcp(%s:%s)/%s?charset=utf8mb4&parseTime=true&loc=Local", user, password, host, port, name), nil
}

func dsnFromURL(raw string) (string, error) {
	raw = strings.ReplaceAll(raw, `\@`, "@")
	parsed, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("parse EVCS_DATABASE_URL: %w", err)
	}
	if parsed.Scheme != "mysql" && parsed.Scheme != "mysql+pymysql" {
		return "", fmt.Errorf("EVCS_DATABASE_URL must use mysql://")
	}
	password, _ := parsed.User.Password()
	database := strings.TrimPrefix(parsed.Path, "/")
	if parsed.Hostname() == "" || parsed.User.Username() == "" || database == "" {
		return "", fmt.Errorf("EVCS_DATABASE_URL is incomplete")
	}
	port := parsed.Port()
	if port == "" {
		port = "3306"
	}
	return fmt.Sprintf(
		"%s:%s@tcp(%s:%s)/%s?charset=utf8mb4&parseTime=true&loc=Local",
		parsed.User.Username(), password, parsed.Hostname(), port, database,
	), nil
}

func envOrDefault(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func envInt(key string, fallback int) int {
	value, err := strconv.Atoi(os.Getenv(key))
	if err != nil {
		return fallback
	}
	return value
}
