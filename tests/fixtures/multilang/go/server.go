package main

import (
	"fmt"
	"os"
	"./util"
)

const StatusDone = "DONE"

type Server struct {
	Port int
}

func NewServer(port int) *Server {
	return &Server{Port: port}
}

func (s *Server) Start() {
	key := os.Getenv("API_KEY")
	fmt.Println(key, StatusDone)
	util.Helper()
}

func main() {
	NewServer(8080).Start()
}
